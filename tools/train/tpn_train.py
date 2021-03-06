#!/usr/bin/env python

import argparse
import sys
import os
import os.path as osp
this_dir = osp.dirname(__file__)
sys.path.insert(0, osp.join(this_dir, '../../external/caffe-mpi/build/install/python'))
sys.path.insert(0, osp.join(this_dir, '../../external/py-faster-rcnn/lib'))
from fast_rcnn.craft import im_detect
from fast_rcnn.config import cfg, cfg_from_file
import caffe
from caffe.proto import caffe_pb2
from mpi4py import MPI
import google.protobuf as protobuf
import yaml
import glob
from vdetlib.utils.protocol import proto_load, frame_path_at
from vdetlib.utils.common import imread
from vdetlib.utils.visual import add_bbox
sys.path.insert(0, osp.join(this_dir, '../../src'))
from tpn.propagate import roi_train_propagation
from tpn.target import add_track_targets
import numpy as np
import cPickle
import random
import cv2

def parse_args():
    parser = argparse.ArgumentParser('TPN training.')
    parser.add_argument('solver')
    parser.add_argument('feature_net')
    parser.add_argument('feature_param')
    parser.add_argument('--train_cfg')
    parser.add_argument('--rcnn_cfg')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--vis_debug', action='store_true')
    parser.add_argument('--bbox_mean', dest='bbox_mean',
                        help='the mean of bbox',
                        default=None, type=str)
    parser.add_argument('--bbox_std', dest='bbox_std',
                        help='the std of bbox',
                        default=None, type=str)
    parser.add_argument('--num_per_batch', dest='num_per_batch',
                        help='Number of boxes in each batch',
                        default=32, type=int)
    restore = parser.add_mutually_exclusive_group()
    restore.add_argument('--weights', type=str, default=None,
        help='RNN trained models.')
    restore.add_argument('--snapshot', type=str, default=None,
        help='RNN solverstates.')
    parser.set_defaults(debug=False, vis_debug=False)
    args = parser.parse_args()
    return args

def vid_valid(vid_name, vid_dir, box_dir, annot_dir, blacklist=[]):
    return osp.isfile(osp.join(vid_dir, vid_name + ".vid")) and \
        osp.isfile(osp.join(box_dir, vid_name + ".box")) and \
        osp.isfile(osp.join(annot_dir, vid_name + ".annot")) and \
        vid_name not in blacklist

def expend_bbox_targets(bbox_targets, class_label, mean, std, num_classes=31):
    bbox_targets = np.asarray(bbox_targets)
    assert bbox_targets.shape == (1,4)
    expend_targets = np.zeros((num_classes, 4), dtype=np.float32)
    if class_label != 0 and class_label != -1:
        expend_targets[class_label,:] = (bbox_targets
            - mean[class_label*4:(class_label+1)*4]) \
            / std[class_label*4:(class_label+1)*4]
    return expend_targets.flatten()[np.newaxis,:]

def expend_bbox_weights(bbox_weights, class_label, num_classes=31):
    bbox_weights = np.asarray(bbox_weights)
    assert bbox_weights.shape == (1,4)
    expend_weights = np.zeros((num_classes, 4), dtype=np.float32)
    if class_label != 0 and class_label != -1:
        expend_weights[class_label,:] = bbox_weights
    return expend_weights.flatten()[np.newaxis,:]

def load_data(config, debug=False):
    if config['blacklist']:
        with open(config['blacklist']) as f:
            blacklist = [line.strip() for line in f]
    else:
        blacklist = []
    # check folder exists
    for folder in ['vid_dir', 'box_dir', 'annot_dir']:
        try:
            assert os.path.isdir(config[folder])
        except:
            raise ValueError("{} folder does not exist: {}".format(folder, config[folder]))

    # preprocess data
    with open(config['vid_list']) as f:
        vid_names = [line.strip().split('/')[-1] for line in f]
    vid_names = [vid_name for vid_name in vid_names if \
        vid_valid(vid_name, config['vid_dir'], config['box_dir'], config['annot_dir'],
                  blacklist)]
    random.shuffle(vid_names)

    if debug:
        vid_names = vid_names[:500]

    print "Loading data..."
    tot_data = {}
    for idx, vid_name in enumerate(vid_names, start=1):
        tot_data[vid_name] = {}
        for key in ['vid', 'box', 'annot']:
            tot_data[vid_name][key] = proto_load(
                osp.join(config[key + '_dir'], "{}.{}".format(vid_name, key)))
        if idx % 500 == 0:
            print "{} samples processed.".format(idx)
    if idx % 500 != 0:
        print "{} samples processed.".format(idx)
    return tot_data, vid_names

def load_nets(args, cur_gpu):
    # initialize solver and feature net,
    # RNN should be initialized before CNN, because CNN cudnn conv layers
    # may assume using all available memory
    caffe.set_mode_gpu()
    caffe.set_device(cur_gpu)
    solver = caffe.SGDSolver(args.solver)
    if args.snapshot:
        print "Restoring history from {}".format(args.snapshot)
        solver.restore(args.snapshot)
    rnn = solver.net
    if args.weights:
        rnn.copy_from(args.weights)
    feature_net = caffe.Net(args.feature_net, args.feature_param, caffe.TEST)

    # apply bbox regression normalization on the net weights
    with open(args.bbox_mean, 'rb') as f:
        bbox_means = cPickle.load(f)
    with open(args.bbox_std, 'rb') as f:
        bbox_stds = cPickle.load(f)
    feature_net.params['bbox_pred_vid'][0].data[...] = \
        feature_net.params['bbox_pred_vid'][0].data * bbox_stds[:, np.newaxis]
    feature_net.params['bbox_pred_vid'][1].data[...] = \
        feature_net.params['bbox_pred_vid'][1].data * bbox_stds + bbox_means
    return solver, feature_net, rnn, bbox_means, bbox_stds

def _pad_array(array, len_first_dim, value=0.):
    num = array.shape[0]
    if num != len_first_dim:
        new_shape = list(array.shape)
        new_shape[0] = len_first_dim
        padded_array = np.ones(new_shape) * value
        padded_array[:num,...] = array
        return padded_array
    else:
        return array

def process_track_results(track_res, vid_proto, annot_proto, bbox_means, bbox_stds
    num_tracks):
    # calculate targets, generate dummy track_proto
    # track_res[0]:
    #   roi: n * 4
    #   frame: int
    #   feat: n * c
    #   bbox: n * num_cls * 4
    track_proto = {}
    track_proto['video'] = vid_proto['video']
    tracks = [[] for _ in xrange(num_tracks)]
    feat = []
    for frame_res in track_res:
        if frame_res['frame'] == -1: break
        frame = frame_res['frame']
        rois = frame_res['roi']
        assert len(tracks) == rois.shape[0]
        for track, roi in zip(tracks, rois):
            track.append(
                {
                    "frame": frame,
                    "roi": roi.tolist()
                }
            )
        feat.append(frame_res['feat'])
    track_proto['tracks'] = tracks
    add_track_targets(track_proto, annot_proto, verbose=False)

    tracks_proto = track_proto['tracks']
    # load data to RNN
    # data: t * (n * c) -> t * n * c
    feat = _pad_array(np.asarray(feat), track_length)
    # cont: t * n
    cont = np.ones(feat.shape[:2])
    cont[0,:] = 0
    # labels: t * n
    labels = np.asarray([[frame['class_label'] for frame in track] \
        for track in tracks_proto]).T.copy()
    labels = _pad_array(labels, track_length, -1)
    # bbox_targets
    bbox_targets = np.asarray([[expend_bbox_targets(frame['bbox_target'],
            frame['class_label'], bbox_means, bbox_stds) for frame in track] \
        for track in tracks_proto]).squeeze(axis=2).swapaxes(0,1).copy()
    bbox_targets = _pad_array(bbox_targets, track_length)
    # bbox_weights
    bbox_weights = np.asarray([[expend_bbox_weights(frame['bbox_weight'],
            frame['class_label']) for frame in track] \
        for track in tracks_proto]).squeeze(axis=2).swapaxes(0,1).copy()
    bbox_weights = _pad_array(bbox_weights, track_length)
    return feat, cont, labels, bbox_targets, bbox_weights

def show_track_res(track_res, vid_proto):
    cv2.namedWindow('tracks')
    for frame_res in track_res:
        if frame_res['frame'] == -1: break
        frame = frame_res['frame']
        img = imread(frame_path_at(vid_proto, frame))
        boxes = frame_res['roi'].tolist()
        tracked = add_bbox(img, boxes, None, None, 2)
        cv2.imshow('tracks', tracked)
        if cv2.waitKey(0) == ord('q'):
            cv2.destroyAllWindows()
            sys.exit(0)
    cv2.destroyAllWindows()

if __name__ == '__main__':
    args = parse_args()

    comm = MPI.COMM_WORLD
    mpi_rank = comm.Get_rank()
    pool_size = comm.Get_size()
    caffe.set_parallel()

    if args.rcnn_cfg is not None:
        cfg_from_file(args.rcnn_cfg)

    # load config file
    with open(args.train_cfg) as f:
        config = yaml.load(f.read())
    print "Config:\n{}".format(config)

    # load data
    tot_data, vid_names = load_data(config, args.debug)

    # read solver file
    solver_param = caffe_pb2.SolverParameter()
    with open(args.solver, 'r') as f:
        protobuf.text_format.Merge(f.read(), solver_param)
    max_iter = solver_param.max_iter

    # get gpu id
    gpus = solver_param.device_id
    assert len(gpus) >= pool_size
    cur_gpu = gpus[mpi_rank]
    cfg.GPU_ID = cur_gpu

    # load solver and nets
    solver, feature_net, rnn, bbox_means, bbox_stds = load_nets(args, cur_gpu)

    # start training
    iter = mpi_rank
    st_iter = solver.iter
    for i in xrange(max_iter - st_iter):
        track_length = config['track_length']
        num_tracks = config['track_per_vid']
        vid_name = vid_names[iter]
        iter += 1
        if iter >= len(vid_names):
            print "Reach end of data, start over."
            iter = 0
        if args.vis_debug:
            print "GPU {}: vid_name {}".format(mpi_rank, vid_name)

        vid_proto = tot_data[vid_name]['vid']
        box_proto = tot_data[vid_name]['box']
        annot_proto = tot_data[vid_name]['annot']

        # tracking
        track_res = roi_train_propagation(vid_proto, box_proto, feature_net,
            det_fun=im_detect,
            num_tracks=num_tracks,
            length=track_length,
            fg_ratio=config['fg_ratio'],
            batch_size=args.num_per_batch)

        if args.vis_debug:
            show_track_res(track_res, vid_proto)

        feat, cont, labels, bbox_targets, bbox_weights = process_track_results(
            track_res, vid_proto, annot_proto,
            bbox_means, bbox_stds, num_tracks)
        rnn.blobs['data'].reshape(*(feat.shape))
        rnn.blobs['data'].data[...] = feat
        rnn.blobs['cont'].reshape(*(cont.shape))
        rnn.blobs['cont'].data[...] = cont
        rnn.blobs['labels'].reshape(*(labels.shape))
        rnn.blobs['labels'].data[...] = labels
        rnn.blobs['bbox_targets'].reshape(*bbox_targets.shape)
        rnn.blobs['bbox_targets'].data[...] = bbox_targets
        rnn.blobs['bbox_weights'].reshape(*bbox_weights.shape)
        rnn.blobs['bbox_weights'].data[...] = bbox_weights

        solver.step(1)
