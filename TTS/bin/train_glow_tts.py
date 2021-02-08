#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import glob
import os
import sys
import time
import traceback
import numpy as np

import torch
from random import randrange
from torch.utils.data import DataLoader
from TTS.tts.datasets.preprocess import load_meta_data
from TTS.tts.datasets.TTSDataset import MyDataset
from TTS.tts.layers.losses import GlowTTSLoss
from TTS.tts.utils.distribute import (DistributedSampler, init_distributed,
                                      reduce_tensor)
from TTS.tts.utils.generic_utils import setup_model, check_config_tts
from TTS.tts.utils.io import save_best_model, save_checkpoint
from TTS.tts.utils.measures import alignment_diagonal_score
from TTS.tts.utils.speakers import parse_speakers, load_speaker_mapping
from TTS.tts.utils.synthesis import synthesis
from TTS.tts.utils.text.symbols import make_symbols, phonemes, symbols
from TTS.tts.utils.visual import plot_alignment, plot_spectrogram
from TTS.utils.audio import AudioProcessor
from TTS.utils.console_logger import ConsoleLogger
from TTS.utils.generic_utils import (KeepAverage, count_parameters,
                                     create_experiment_folder, get_git_branch,
                                     remove_experiment_folder, set_init_dict)
from TTS.utils.io import copy_config_file, load_config
from TTS.utils.radam import RAdam
from TTS.utils.tensorboard_logger import TensorboardLogger
from TTS.utils.training import (NoamLR, check_update,
                                setup_torch_training_env)

use_cuda, num_gpus = setup_torch_training_env(True, False)

def setup_loader(ap, r, is_val=False, verbose=False, speaker_mapping=None):
    if is_val and not c.run_eval:
        loader = None
    else:
        dataset = MyDataset(
            r,
            c.text_cleaner,
            compute_linear_spec=False,
            meta_data=meta_data_eval if is_val else meta_data_train,
            ap=ap,
            tp=c.characters if 'characters' in c.keys() else None,
            add_blank=c['add_blank'] if 'add_blank' in c.keys() else False,
            batch_group_size=0 if is_val else c.batch_group_size *
            c.batch_size,
            min_seq_len=c.min_seq_len,
            max_seq_len=c.max_seq_len,
            phoneme_cache_path=c.phoneme_cache_path,
            use_phonemes=c.use_phonemes,
            phoneme_language=c.phoneme_language,
            enable_eos_bos=c.enable_eos_bos_chars,
            verbose=verbose,
            speaker_mapping=speaker_mapping if c.use_speaker_embedding and c.use_external_speaker_embedding_file else None)
        
        sampler = DistributedSampler(dataset) if num_gpus > 1 else None
        
        if getattr(c, "speaker_balancer_batch", False) and sampler is None and not is_val:
            print("Using Speaker Balancer Batch")
            # get speakers names
            speaker_names = np.array([item[2] for item in dataset.items])

            unique_speaker_names = np.unique(speaker_names).tolist()
            speaker_ids = [unique_speaker_names.index(s) for s in speaker_names]

            # count number samples by speaker
            speaker_count = np.array([len(np.where(speaker_names == s)[0]) for s in unique_speaker_names])

            # create weight
            weight = 1. / speaker_count
            samples_weight = np.array([weight[s] for s in speaker_ids])
            speakers_dataset_samples_weight = torch.from_numpy(samples_weight).double()
            # create sampler
            sampler = torch.utils.data.sampler.WeightedRandomSampler(speakers_dataset_samples_weight, len(speakers_dataset_samples_weight))

        loader = DataLoader(
            dataset,
            batch_size=c.eval_batch_size if is_val else c.batch_size,
            shuffle=False,
            collate_fn=dataset.collate_fn,
            drop_last=False,
            sampler=sampler,
            num_workers=c.num_val_loader_workers
            if is_val else c.num_loader_workers,
            pin_memory=False)
        
        # test loader
        '''for _, data in enumerate(loader):
            # format data
            speaker_ids = np.array(data[2])
            freqs = [len(np.where(speaker_ids == s_id)[0] ) for s_id in unique_speaker_names]
            print(speaker_ids[0], freqs)
        
        exit()'''

    return loader

def format_data(data, speaker_mapping=None):

    # setup input data
    text_input = data[0]
    text_lengths = data[1]
    speaker_names = data[2]
    mel_input = data[4].permute(0, 2, 1)  # B x D x T
    mel_lengths = data[5]
    attn_mask = data[9]
    avg_text_length = torch.mean(text_lengths.float())
    avg_spec_length = torch.mean(mel_lengths.float())

    if c.use_speaker_embedding:
        if c.use_external_speaker_embedding_file:
            speaker_ids = data[8]
        else:
            speaker_ids = [
                speaker_mapping[speaker_name] for speaker_name in speaker_names
            ]
            speaker_ids = torch.LongTensor(speaker_ids)
    else:
        speaker_ids = None

    # dispatch data to GPU
    if use_cuda:
        text_input = text_input.cuda(non_blocking=True)
        text_lengths = text_lengths.cuda(non_blocking=True)
        mel_input = mel_input.cuda(non_blocking=True)
        mel_lengths = mel_lengths.cuda(non_blocking=True)
        if speaker_ids is not None:
            speaker_ids = speaker_ids.cuda(non_blocking=True)
        if attn_mask is not None:
            attn_mask = attn_mask.cuda(non_blocking=True)
    return text_input, text_lengths, mel_input, mel_lengths, speaker_ids,\
         avg_text_length, avg_spec_length, attn_mask


def data_depended_init(model, ap, speaker_mapping=None):
    """Data depended initialization for activation normalization."""
    if hasattr(model, 'module'):
        for f in model.module.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(True)
    else:
        for f in model.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(True)

    data_loader = setup_loader(ap, 1, is_val=False, speaker_mapping=speaker_mapping)
    model.train()
    print(" > Data depended initialization ... ")
    with torch.no_grad():
        for _, data in enumerate(data_loader):

            # format data
            text_input, text_lengths, mel_input, mel_lengths, speaker_ids,\
                _, _, attn_mask = format_data(data, speaker_mapping=speaker_mapping)

            # forward pass model
            _ = model.forward(
                text_input, text_lengths, mel_input, mel_lengths, attn_mask, g=speaker_ids)
            break

    if hasattr(model, 'module'):
        for f in model.module.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(False)
    else:
        for f in model.decoder.flows:
            if getattr(f, "set_ddi", False):
                f.set_ddi(False)
    return model


def train(model, data_loader, criterion, optimizer, scheduler,
          ap, global_step, epoch, amp, speaker_mapping=None):
    model.train()
    epoch_time = 0
    keep_avg = KeepAverage()
    if use_cuda:
        batch_n_iter = int(
            len(data_loader.dataset) / (c.batch_size * num_gpus))
    else:
        batch_n_iter = int(len(data_loader.dataset) / c.batch_size)
    end_time = time.time()
    c_logger.print_train_start()
    for num_iter, data in enumerate(data_loader):
        start_time = time.time()

        # format data
        text_input, text_lengths, mel_input, mel_lengths, speaker_ids,\
            avg_text_length, avg_spec_length, attn_mask = format_data(data, speaker_mapping=speaker_mapping)

        loader_time = time.time() - end_time

        global_step += 1

        # setup lr
        if c.noam_schedule:
            scheduler.step()
        optimizer.zero_grad()

        # forward pass model
        z, logdet, y_mean, y_log_scale, alignments, o_dur_log, o_total_dur, mu, logvar = model.forward(
            text_input, text_lengths, mel_input, mel_lengths, attn_mask, g=speaker_ids)

        # compute loss
        loss_dict = criterion(z, y_mean, y_log_scale, logdet, mel_lengths,
                              o_dur_log, o_total_dur, text_lengths, mu, logvar, global_step)

        # backward pass
        if amp is not None:
            with amp.scale_loss(loss_dict['loss'], optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss_dict['loss'].backward()

        if amp:
            amp_opt_params = amp.master_params(optimizer)
        else:
            amp_opt_params = None
        grad_norm, _ = check_update(model, c.grad_clip, ignore_stopnet=True, amp_opt_params=amp_opt_params)
        optimizer.step()

        # current_lr
        current_lr = optimizer.param_groups[0]['lr']

        # compute alignment error (the lower the better )
        align_error = 1 - alignment_diagonal_score(alignments)
        loss_dict['align_error'] = align_error

        step_time = time.time() - start_time
        epoch_time += step_time

        # aggregate losses from processes
        if num_gpus > 1:
            loss_dict['log_mle'] = reduce_tensor(loss_dict['log_mle'].data, num_gpus)
            loss_dict['loss_dur'] = reduce_tensor(loss_dict['loss_dur'].data, num_gpus)
            loss_dict['loss'] = reduce_tensor(loss_dict['loss'] .data, num_gpus)

        # detach loss values
        loss_dict_new = dict()
        for key, value in loss_dict.items():
            if isinstance(value, (int, float)):
                loss_dict_new[key] = value
            else:
                loss_dict_new[key] = value.item()
        loss_dict = loss_dict_new

        # update avg stats
        update_train_values = dict()
        for key, value in loss_dict.items():
            update_train_values['avg_' + key] = value
        update_train_values['avg_loader_time'] = loader_time
        update_train_values['avg_step_time'] = step_time
        keep_avg.update_values(update_train_values)

        # print training progress
        if global_step % c.print_step == 0:
            log_dict = {
                "avg_spec_length": [avg_spec_length, 1],  # value, precision
                "avg_text_length": [avg_text_length, 1],
                "step_time": [step_time, 4],
                "loader_time": [loader_time, 2],
                "current_lr": current_lr,
            }
            c_logger.print_train_step(batch_n_iter, num_iter, global_step,
                                      log_dict, loss_dict, keep_avg.avg_values)

        if args.rank == 0:
            # Plot Training Iter Stats
            # reduce TB load
            if global_step % c.tb_plot_step == 0:
                iter_stats = {
                    "lr": current_lr,
                    "grad_norm": grad_norm,
                    "step_time": step_time
                }
                iter_stats.update(loss_dict)
                tb_logger.tb_train_iter_stats(global_step, iter_stats)

            if global_step % c.save_step == 0:
                if c.checkpoint:
                    # save model
                    save_checkpoint(model, optimizer, global_step, epoch, 1, OUT_PATH,
                                    model_loss=loss_dict['loss'],
                                    amp_state_dict=amp.state_dict() if amp else None)

                # Diagnostic visualizations
                # direct pass on model for spec predictions
                spec_pred, *_ = model.inference(text_input[:1], text_lengths[:1], g=speaker_ids[:1])
                spec_pred = spec_pred.permute(0, 2, 1)
                gt_spec = mel_input.permute(0, 2, 1)
                const_spec = spec_pred[0].data.cpu().numpy()
                gt_spec = gt_spec[0].data.cpu().numpy()
                align_img = alignments[0].data.cpu().numpy()

                figures = {
                    "prediction": plot_spectrogram(const_spec, ap),
                    "ground_truth": plot_spectrogram(gt_spec, ap),
                    "alignment": plot_alignment(align_img),
                }

                tb_logger.tb_train_figures(global_step, figures)

                # Sample audio
                train_audio = ap.inv_melspectrogram(const_spec.T)
                tb_logger.tb_train_audios(global_step,
                                          {'TrainAudio': train_audio},
                                          c.audio["sample_rate"])
        end_time = time.time()

    # print epoch stats
    c_logger.print_train_epoch_end(global_step, epoch, epoch_time, keep_avg)

    # Plot Epoch Stats
    if args.rank == 0:
        epoch_stats = {"epoch_time": epoch_time}
        epoch_stats.update(keep_avg.avg_values)
        tb_logger.tb_train_epoch_stats(global_step, epoch_stats)
        if c.tb_model_param_stats:
            tb_logger.tb_model_weights(model, global_step)
    return keep_avg.avg_values, global_step


@torch.no_grad()
def evaluate(model, data_loader, criterion, ap, global_step, epoch, speaker_mapping):
    model.eval()
    epoch_time = 0
    keep_avg = KeepAverage()
    c_logger.print_eval_start()
    if data_loader is not None:
        for num_iter, data in enumerate(data_loader):
            start_time = time.time()

            # format data
            text_input, text_lengths, mel_input, mel_lengths, speaker_ids,\
                _, _, attn_mask = format_data(data, speaker_mapping=speaker_mapping)

            # forward pass model
            z, logdet, y_mean, y_log_scale, alignments, o_dur_log, o_total_dur, mu, logvar = model.forward(
                text_input, text_lengths, mel_input, mel_lengths, attn_mask, g=speaker_ids)

            # compute loss
            loss_dict = criterion(z, y_mean, y_log_scale, logdet, mel_lengths,
                                  o_dur_log, o_total_dur, text_lengths, mu, logvar, global_step)

            # step time
            step_time = time.time() - start_time
            epoch_time += step_time

            # compute alignment score
            align_error = 1 - alignment_diagonal_score(alignments)
            loss_dict['align_error'] = align_error

            # aggregate losses from processes
            if num_gpus > 1:
                loss_dict['log_mle'] = reduce_tensor(loss_dict['log_mle'].data, num_gpus)
                loss_dict['loss_dur'] = reduce_tensor(loss_dict['loss_dur'].data, num_gpus)
                loss_dict['loss'] = reduce_tensor(loss_dict['loss'] .data, num_gpus)

            # detach loss values
            loss_dict_new = dict()
            for key, value in loss_dict.items():
                if isinstance(value, (int, float)):
                    loss_dict_new[key] = value
                else:
                    loss_dict_new[key] = value.item()
            loss_dict = loss_dict_new

            # update avg stats
            update_train_values = dict()
            for key, value in loss_dict.items():
                update_train_values['avg_' + key] = value
            keep_avg.update_values(update_train_values)

            if c.print_eval:
                c_logger.print_eval_step(num_iter, loss_dict, keep_avg.avg_values)

        if args.rank == 0:
            # Diagnostic visualizations
            # direct pass on model for spec predictions
            if hasattr(model, 'module'):
                spec_pred, *_ = model.module.inference(text_input[:1], text_lengths[:1], g=speaker_ids[:1], style_mel=mel_input[:1])
            else:
                spec_pred, *_ = model.inference(text_input[:1], text_lengths[:1], g=speaker_ids[:1], style_mel=mel_input[:1])
            spec_pred = spec_pred.permute(0, 2, 1)
            gt_spec = mel_input.permute(0, 2, 1)

            const_spec = spec_pred[0].data.cpu().numpy()
            gt_spec = gt_spec[0].data.cpu().numpy()
            align_img = alignments[0].data.cpu().numpy()

            eval_figures = {
                "prediction": plot_spectrogram(const_spec, ap),
                "ground_truth": plot_spectrogram(gt_spec, ap),
                "alignment": plot_alignment(align_img)
            }

            # Sample audio
            eval_audio = ap.inv_melspectrogram(const_spec.T)
            tb_logger.tb_eval_audios(global_step, {"ValAudio": eval_audio},
                                     c.audio["sample_rate"])

            # Plot Validation Stats
            tb_logger.tb_eval_stats(global_step, keep_avg.avg_values)
            tb_logger.tb_eval_figures(global_step, eval_figures)

    if args.rank == 0 and epoch >= c.test_delay_epochs:
        if c.test_sentences_file is None:
            test_sentences = [
                "It took me quite a long time to develop a voice, and now that I have it I'm not going to be silent.",
                "Be a voice, not an echo.",
                "I'm sorry Dave. I'm afraid I can't do that.",
                "This cake is great. It's so delicious and moist.",
                "Prior to November 22, 1963."
            ]
        else:
            with open(c.test_sentences_file, "r") as f:
                test_sentences = [s.strip() for s in f.readlines()]

        # test sentences
        test_audios = {}
        test_figures = {}
        print(" | > Synthesizing test sentences")
        if c.use_speaker_embedding:
            if c.use_external_speaker_embedding_file:
                speaker_embedding = speaker_mapping[list(speaker_mapping.keys())[randrange(len(speaker_mapping)-1)]]['embedding']
                speaker_id = None
            else:
                speaker_id = 0
                speaker_embedding = None
        else:
            speaker_id = None
            speaker_embedding = None

        style_wav = c.get("style_wav_for_test")
        if style_wav is None and c.use_gst:
            # inicialize GST with zero dict.
            style_wav = {}
            print("WARNING: You don't provided a gst style wav, for this reason we use a zero tensor!")
            for i in range(c.gst['gst_style_tokens']):
                style_wav[str(i)] = 0

        for idx, test_sentence in enumerate(test_sentences):
            try:
                wav, alignment, _, postnet_output, _, _ = synthesis(
                    model,
                    test_sentence,
                    c,
                    use_cuda,
                    ap,
                    speaker_id=speaker_id,
                    speaker_embedding=speaker_embedding,
                    style_wav=style_wav,
                    truncated=False,
                    enable_eos_bos_chars=c.enable_eos_bos_chars, #pylint: disable=unused-argument
                    use_griffin_lim=True,
                    do_trim_silence=False)

                file_path = os.path.join(AUDIO_PATH, str(global_step))
                os.makedirs(file_path, exist_ok=True)
                file_path = os.path.join(file_path,
                                         "TestSentence_{}.wav".format(idx))
                ap.save_wav(wav, file_path)
                test_audios['{}-audio'.format(idx)] = wav
                test_figures['{}-prediction'.format(idx)] = plot_spectrogram(
                    postnet_output, ap)
                test_figures['{}-alignment'.format(idx)] = plot_alignment(
                    alignment)
            except: #pylint: disable=bare-except
                print(" !! Error creating Test Sentence -", idx)
                traceback.print_exc()
        tb_logger.tb_test_audios(global_step, test_audios,
                                 c.audio['sample_rate'])
        tb_logger.tb_test_figures(global_step, test_figures)
    return keep_avg.avg_values


# FIXME: move args definition/parsing inside of main?
def main(args):  # pylint: disable=redefined-outer-name
    # pylint: disable=global-variable-undefined
    global meta_data_train, meta_data_eval, symbols, phonemes
    # Audio processor
    ap = AudioProcessor(**c.audio)
    if 'characters' in c.keys():
        symbols, phonemes = make_symbols(**c.characters)

    # DISTRUBUTED
    if num_gpus > 1:
        init_distributed(args.rank, num_gpus, args.group_id,
                         c.distributed["backend"], c.distributed["url"])
    num_chars = len(phonemes) if c.use_phonemes else len(symbols)

    # load data instances
    meta_data_train, meta_data_eval = load_meta_data(c.datasets)

    # set the portion of the data used for training
    if 'train_portion' in c.keys():
        meta_data_train = meta_data_train[:int(len(meta_data_train) * c.train_portion)]
    if 'eval_portion' in c.keys():
        meta_data_eval = meta_data_eval[:int(len(meta_data_eval) * c.eval_portion)]

    # parse speakers
    num_speakers, speaker_embedding_dim, speaker_mapping = parse_speakers(c, args, meta_data_train, OUT_PATH)
    # print(speaker_mapping.keys())
    # setup model
    model = setup_model(num_chars, num_speakers, c, speaker_embedding_dim=speaker_embedding_dim)
    optimizer = RAdam(model.parameters(), lr=c.lr, weight_decay=0, betas=(0.9, 0.98), eps=1e-9)
    criterion = GlowTTSLoss(use_vae=getattr(c, "use_vae", False))

    if c.apex_amp_level:
        # pylint: disable=import-outside-toplevel
        from apex import amp
        from apex.parallel import DistributedDataParallel as DDP
        model.cuda()
        model, optimizer = amp.initialize(model, optimizer, opt_level=c.apex_amp_level)
    else:
        amp = None

    if args.restore_path:
        checkpoint = torch.load(args.restore_path, map_location='cpu')
        try:
            # TODO: fix optimizer init, model.cuda() needs to be called before
            # optimizer restore
            if not c.noam_schedule:
                optimizer.load_state_dict(checkpoint['optimizer'])
            if c.reinit_layers:
                raise RuntimeError
            model.load_state_dict(checkpoint['model'])
        except: #pylint: disable=bare-except
            print(" > Partial model initialization.")
            model_dict = model.state_dict()
            model_dict = set_init_dict(model_dict, checkpoint['model'], c)
            model.load_state_dict(model_dict)
            del model_dict

        if amp and 'amp' in checkpoint:
            amp.load_state_dict(checkpoint['amp'])

        for group in optimizer.param_groups:
            group['initial_lr'] = c.lr
        print(" > Model restored from step %d" % checkpoint['step'],
              flush=True)
        args.restore_step = checkpoint['step']
    else:
        args.restore_step = 0

    if use_cuda:
        model.cuda()
        criterion.cuda()

    # DISTRUBUTED
    if num_gpus > 1:
        model = DDP(model)

    if c.noam_schedule:
        scheduler = NoamLR(optimizer,
                           warmup_steps=c.warmup_steps,
                           last_epoch=args.restore_step - 1)
    else:
        scheduler = None

    num_params = count_parameters(model)
    print("\n > Model has {} parameters".format(num_params), flush=True)

    if 'best_loss' not in locals():
        best_loss = float('inf')

    global_step = args.restore_step
    # ignore data depend inicialization if continue training
    if global_step == 0:
        model = data_depended_init(model, ap, speaker_mapping)

    train_data_loader = setup_loader(ap, 1, is_val=False,
                               verbose=True, speaker_mapping=speaker_mapping)
    eval_data_loader = setup_loader(ap, 1, is_val=True, speaker_mapping=speaker_mapping)

    for epoch in range(0, c.epochs):
        c_logger.print_epoch_start(epoch, c.epochs)
        train_avg_loss_dict, global_step = train(model, train_data_loader, criterion, optimizer,
                                                 scheduler, ap, global_step,
                                                 epoch, amp, speaker_mapping)
        eval_avg_loss_dict = evaluate(model, eval_data_loader, criterion, ap, global_step, epoch, speaker_mapping=speaker_mapping)
        c_logger.print_epoch_end(epoch, eval_avg_loss_dict)
        target_loss = train_avg_loss_dict['avg_loss']
        if c.run_eval:
            target_loss = eval_avg_loss_dict['avg_loss']
        best_loss = save_best_model(target_loss, best_loss, model, optimizer, global_step, epoch, c.r,
                                    OUT_PATH, amp_state_dict=amp.state_dict() if amp else None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--continue_path',
        type=str,
        help='Training output folder to continue training. Use to continue a training. If it is used, "config_path" is ignored.',
        default='',
        required='--config_path' not in sys.argv)
    parser.add_argument(
        '--restore_path',
        type=str,
        help='Model file to be restored. Use to finetune a model.',
        default='')
    parser.add_argument(
        '--config_path',
        type=str,
        help='Path to config file for training.',
        required='--continue_path' not in sys.argv
    )
    parser.add_argument('--debug',
                        type=bool,
                        default=False,
                        help='Do not verify commit integrity to run training.')

    # DISTRUBUTED
    parser.add_argument(
        '--rank',
        type=int,
        default=0,
        help='DISTRIBUTED: process rank for distributed training.')
    parser.add_argument('--group_id',
                        type=str,
                        default="",
                        help='DISTRIBUTED: process group id.')
    args = parser.parse_args()

    if args.continue_path != '':
        args.output_path = args.continue_path
        args.config_path = os.path.join(args.continue_path, 'config.json')
        list_of_files = glob.glob(args.continue_path + "/*.pth.tar") # * means all if need specific format then *.csv
        latest_model_file = max(list_of_files, key=os.path.getctime)
        args.restore_path = latest_model_file
        print(f" > Training continues for {args.restore_path}")

    # setup output paths and read configs
    c = load_config(args.config_path)
    # check_config(c)
    check_config_tts(c)
    _ = os.path.dirname(os.path.realpath(__file__))

    if c.apex_amp_level:
        print("   >  apex AMP level: ", c.apex_amp_level)

    OUT_PATH = args.continue_path
    if args.continue_path == '':
        OUT_PATH = create_experiment_folder(c.output_path, c.run_name, args.debug)

    AUDIO_PATH = os.path.join(OUT_PATH, 'test_audios')

    c_logger = ConsoleLogger()

    if args.rank == 0:
        os.makedirs(AUDIO_PATH, exist_ok=True)
        new_fields = {}
        if args.restore_path:
            new_fields["restore_path"] = args.restore_path
        new_fields["github_branch"] = get_git_branch()
        copy_config_file(args.config_path,
                         os.path.join(OUT_PATH, 'config.json'), new_fields)
        os.chmod(AUDIO_PATH, 0o775)
        os.chmod(OUT_PATH, 0o775)

        LOG_DIR = OUT_PATH
        tb_logger = TensorboardLogger(LOG_DIR, model_name='TTS')

        # write model desc to tensorboard
        tb_logger.tb_add_text('model-description', c['run_description'], 0)

    try:
        main(args)
    except KeyboardInterrupt:
        remove_experiment_folder(OUT_PATH)
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        remove_experiment_folder(OUT_PATH)
        traceback.print_exc()
        sys.exit(1)
