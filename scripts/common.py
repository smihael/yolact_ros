#!/usr/bin/env python
import roslib
roslib.load_manifest('yolact_ros')
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "yolact"))
import rospy
import rospkg
import cv2
import threading
from queue import Queue
from std_msgs.msg import String
from std_msgs.msg import Header
from sensor_msgs.msg import Image
from sensor_msgs.msg import CompressedImage
from yolact_ros_msgs.msg import Detections
from yolact_ros_msgs.msg import Detection
from yolact_ros_msgs.msg import Box
from yolact_ros_msgs.msg import Mask

from yolact_ros_msgs import mask_utils

from cv_bridge import CvBridge, CvBridgeError

import numpy as np
import torch
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
from yolact import Yolact
from utils.augmentations import BaseTransform, FastBaseTransform, Resize
from layers.output_utils import postprocess, undo_image_transformation
from data import COCODetection, get_label_map, MEANS, COLORS
from data import cfg, set_cfg, set_dataset
from utils import timer
from utils.functions import SavePath
from collections import defaultdict
from rospy.numpy_msg import numpy_msg

from dynamic_reconfigure.server import Server as ReconfigureServer
from yolact_ros.cfg import YolactConfig

iou_thresholds = [x / 100 for x in range(50, 100, 5)]
coco_cats = {} # Call prep_coco_cats to fill this
coco_cats_inv = {}
color_cache = defaultdict(lambda: {})

class SynchronizedObject:
  def __init__(self):
    self.obj = None
    self.co = threading.Condition()

  def put_nowait(self, obj):
    self.co.acquire()
    self.obj = obj
    self.co.notify()
    self.co.release()

  def get_nowait(self):
    self.co.acquire()
    obj = self.obj
    self.obj = None
    self.co.release()
    return obj

  def get(self):
    self.co.acquire()
    while self.obj is None:
      self.co.wait()
    obj = self.obj
    self.obj = None
    self.co.release()
    return obj

class YolactNode:

  def __init__(self, net:Yolact):
    self.net = net

    self.image_pub = rospy.Publisher("~visualization", Image, queue_size=1)

    self.detections_pub = rospy.Publisher("~detections", Detections, queue_size=1)

    self.bridge = CvBridge()

    self.processing_queue = SynchronizedObject()
    self.processing_thread = threading.Thread(target=self.processingLoop)
    self.processing_thread.daemon = True
    self.processing_thread.start()

    self.image_vis_queue = SynchronizedObject()
    self.visualization_thread = None
    self.unpause_visualization = threading.Event()

    # set parameter default values (will be overwritten by dynamic reconfigure callback)

    self.image_topic = ''
    self.use_compressed_image = False
    self.publish_visualization = True
    self.publish_detections = True
    self.display_visualization = False

    self.display_masks = True
    self.display_bboxes = True
    self.display_text = True
    self.display_scores = True
    self.display_fps = False
    self.score_threshold = 0.0
    self.crop_masks = True
    self.top_k = 5

    self.image_sub = None # subscriber is created in dynamic_reconfigure callback

    # for counting fps
    self.fps = 0
    self.last_reset_time = rospy.Time()
    self.frame_counter = 0

  def visualizationLoop(self):
      print('Creating cv2 window')
      window_name = 'Segmentation results'
      cv2.namedWindow(window_name)
      print('Window successfully created')
      while True:
        if not self.unpause_visualization.is_set():
            print('Pausing visualization')
            cv2.destroyWindow(window_name)
            cv2.waitKey(30)
            self.unpause_visualization.wait()
            print('Unpausing visualization')
            cv2.namedWindow(window_name)

        image = self.image_vis_queue.get_nowait()
        if image is None:
            cv2.waitKey(30)
            continue

        cv2.imshow(window_name, image)
        cv2.waitKey(30)

  def processingLoop(self):
      while True:
        cv_image, image_header = self.processing_queue.get()
        self.evalimage(cv_image, image_header)


  """
  The functions postprocess_results and prep_display are slightly modified versions
  of the prep_display function in yolact's eval.py; Copyright (c) 2019 Daniel Bolya
  """

  def postprocess_results(self, dets_out, w, h):
      with timer.env('Postprocess'):
          save = cfg.rescore_bbox
          cfg.rescore_bbox = True
          t = postprocess(dets_out, w, h, visualize_lincomb = False,
                                          crop_masks        = self.crop_masks,
                                          score_threshold   = self.score_threshold)
          cfg.rescore_bbox = save

      with timer.env('Copy'):
          idx = t[1].argsort(0, descending=True)[:self.top_k]

          if cfg.eval_mask_branch:
              # Masks are drawn on the GPU, so don't copy
              masks = t[3][idx]
          classes, scores, boxes = [x[idx].cpu().numpy() for x in t[:3]]

      return classes, scores, boxes, masks


  def prep_display(self, classes, scores, boxes, masks, img, class_color=False, mask_alpha=0.45, fps_str=''):

      img_gpu = img / 255.0

      num_dets_to_consider = min(self.top_k, classes.shape[0])
      for j in range(num_dets_to_consider):
          if scores[j] < self.score_threshold:
              num_dets_to_consider = j
              break

      # Quick and dirty lambda for selecting the color for a particular index
      # Also keeps track of a per-gpu color cache for maximum speed
      def get_color(j, on_gpu=None):
          global color_cache
          color_idx = (classes[j] * 5 if class_color else j * 5) % len(COLORS)

          if on_gpu is not None and color_idx in color_cache[on_gpu]:
              return color_cache[on_gpu][color_idx]
          else:
              color = COLORS[color_idx]
              # The image might come in as RGB or BRG, depending
              color = (color[2], color[1], color[0])
              if on_gpu is not None:
                  color = torch.Tensor(color).to(on_gpu).float() / 255.
                  color_cache[on_gpu][color_idx] = color
              return color

      # First, draw the masks on the GPU where we can do it really fast
      # Beware: very fast but possibly unintelligible mask-drawing code ahead
      # I wish I had access to OpenGL or Vulkan but alas, I guess Pytorch tensor operations will have to suffice
      if self.display_masks and cfg.eval_mask_branch and num_dets_to_consider > 0:
          # After this, mask is of size [num_dets, h, w, 1]
          masks = masks[:num_dets_to_consider, :, :, None]

          # Prepare the RGB images for each mask given their color (size [num_dets, h, w, 1])
          if torch.cuda.is_available():
              colors = torch.cat([get_color(j, on_gpu=img_gpu.device.index).view(1, 1, 1, 3) for j in range(num_dets_to_consider)], dim=0)
          else:
              colors = torch.cat([torch.FloatTensor(get_color(j, on_gpu=img.device.index)).view(1, 1, 1, 3) for j in range(num_dets_to_consider)], dim=0)
        
          masks_color = masks.repeat(1, 1, 1, 3) * colors * mask_alpha

          # This is 1 everywhere except for 1-mask_alpha where the mask is
          inv_alph_masks = masks * (-mask_alpha) + 1

          # I did the math for this on pen and paper. This whole block should be equivalent to:
          #    for j in range(num_dets_to_consider):
          #        img_gpu = img_gpu * inv_alph_masks[j] + masks_color[j]
          masks_color_summand = masks_color[0]
          if num_dets_to_consider > 1:
              inv_alph_cumul = inv_alph_masks[:(num_dets_to_consider-1)].cumprod(dim=0)
              masks_color_cumul = masks_color[1:] * inv_alph_cumul
              masks_color_summand += masks_color_cumul.sum(dim=0)

          img_gpu = img_gpu * inv_alph_masks.prod(dim=0) + masks_color_summand

      if self.display_fps:
              # Draw the box for the fps on the GPU
          font_face = cv2.FONT_HERSHEY_DUPLEX
          font_scale = 0.6
          font_thickness = 1

          text_w, text_h = cv2.getTextSize(fps_str, font_face, font_scale, font_thickness)[0]

          img_gpu[0:text_h+8, 0:text_w+8] *= 0.6 # 1 - Box alpha


      # Then draw the stuff that needs to be done on the cpu
      # Note, make sure this is a uint8 tensor or opencv will not anti alias text for whatever reason
      img_numpy = (img_gpu * 255).byte().cpu().numpy()

      if self.display_fps:
          # Draw the text on the CPU
          text_pt = (4, text_h + 2)
          text_color = [255, 255, 255]

          cv2.putText(img_numpy, fps_str, text_pt, font_face, font_scale, text_color, font_thickness, cv2.LINE_AA)

      if num_dets_to_consider == 0:
          return img_numpy

      if self.display_text or self.display_bboxes:
          for j in reversed(range(num_dets_to_consider)):
              x1, y1, x2, y2 = boxes[j, :]
              color = get_color(j)
              score = scores[j]

              if self.display_bboxes:
                  cv2.rectangle(img_numpy, (x1, y1), (x2, y2), color, 1)

              if self.display_text:
                  _class = cfg.dataset.class_names[classes[j]]
                  text_str = '%s: %.2f' % (_class, score) if self.display_scores else _class

                  font_face = cv2.FONT_HERSHEY_DUPLEX
                  font_scale = 0.6
                  font_thickness = 1

                  text_w, text_h = cv2.getTextSize(text_str, font_face, font_scale, font_thickness)[0]

                  text_pt = (x1, y1 - 3)
                  text_color = [255, 255, 255]

                  cv2.rectangle(img_numpy, (x1, y1), (x1 + text_w, y1 - text_h - 4), color, -1)
                  cv2.putText(img_numpy, text_str, text_pt, font_face, font_scale, text_color, font_thickness, cv2.LINE_AA)


      return img_numpy


  def evalimage(self, cv_image, image_header):
    with torch.no_grad():
      if torch.cuda.is_available():
        frame = torch.from_numpy(cv_image).cuda().float()
      else:
        frame = torch.from_numpy(cv_image).float()
      
      batch = FastBaseTransform()(frame.unsqueeze(0))
      preds = self.net(batch)

      h, w, _ = frame.shape
      classes, scores, boxes, masks = self.postprocess_results(preds, w, h)

      if self.display_fps:
        now = rospy.get_rostime()
        if now - self.last_reset_time > rospy.Duration(1): # reset timer / counter every second
          self.fps = self.frame_counter
          self.last_reset_time = now
          self.frame_counter = 0
        self.frame_counter += 1

      if self.publish_visualization or self.display_visualization:
        image = self.prep_display(classes, scores, boxes, masks, frame, fps_str=str(self.fps))

      if self.publish_detections:
        dets = self.generate_detections_msg(classes, scores, boxes, masks, image_header)
        self.detections_pub.publish(dets)

      if self.display_visualization:
        self.image_vis_queue.put_nowait(image)

      if self.publish_visualization:
        try:
          self.image_pub.publish(self.bridge.cv2_to_imgmsg(image, "bgr8"))
        except CvBridgeError as e:
          print(e)

  def callback(self, data):
    try:
      if self.use_compressed_image:
          np_arr = np.fromstring(data.data, np.uint8)
          cv_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
      else:
          cv_image = self.bridge.imgmsg_to_cv2(data, "bgr8")

    except CvBridgeError as e:
      print(e)

    self.processing_queue.put_nowait((cv_image, data.header))

  def reconfigure_callback(self, config, level):
    if level & (1 << 0): # image_topic / use_compressed_image
        if hasattr(self,"image_sub"):
            if self.image_sub is not None:
              self.image_sub.unregister()

            self.use_compressed_image = config.use_compressed_image

            if self.use_compressed_image:
                self.image_topic = config.image_topic + '/compressed'
                self.image_sub = rospy.Subscriber(self.image_topic, CompressedImage, self.callback, queue_size=1, buff_size=2**24)
            else:
                self.image_topic = config.image_topic
                self.image_sub = rospy.Subscriber(self.image_topic, Image, self.callback, queue_size=1, buff_size=2**24)
            print('Subscribed to ' + self.image_topic)

    if level & (1 << 1): # publish_visualization
        self.publish_visualization = config.publish_visualization
    if level & (1 << 2): # publish_detections
        self.publish_detections = config.publish_detections
    if level & (1 << 3): # display_visualization
        self.display_visualization = config.display_visualization
        if self.display_visualization:
            self.unpause_visualization.set()
            if self.visualization_thread is None: # first time visualization
                print('Creating thread')
                self.visualization_thread = threading.Thread(target=self.visualizationLoop)
                self.visualization_thread.daemon = True
                self.visualization_thread.start()
                print('Thread was started')
        else:
            self.unpause_visualization.clear()

    if level & (1 << 4): # display_masks
        self.display_masks = config.display_masks
    if level & (1 << 5): # display_bboxes
        self.display_bboxes = config.display_bboxes
    if level & (1 << 6): # display_text
        self.display_text = config.display_text
    if level & (1 << 7): # display_scores
        self.display_scores = config.display_scores
    if level & (1 << 8): # display_fps
        self.display_fps = config.display_fps
    if level & (1 << 9): # score_threshold
        self.score_threshold = config.score_threshold
    if level & (1 << 10): # crop_masks
        self.crop_masks = config.crop_masks
    if level & (1 << 11): # top_k
        self.top_k = config.top_k

    return config

def main(ros_node):
  rospy.init_node('yolact_ros')
  rospack = rospkg.RosPack()
  yolact_path = rospack.get_path('yolact_ros')
  
  model_path_str = rospy.get_param('~model_path', os.path.join(yolact_path, "scripts/yolact/weights/yolact_base_54_800000.pth"))
  model_path = SavePath.from_str(model_path_str)
  #set_cfg(model_path.model_name + '_config')
  config_str = rospy.get_param('~config',model_path.model_name + '_config')
  set_cfg(config_str)
  
  with torch.no_grad():
      cudnn.benchmark = True
      cudnn.fastest = True
      if torch.cuda.is_available():
          torch.set_default_tensor_type('torch.cuda.FloatTensor')   
      else:
          torch.set_default_tensor_type('torch.FloatTensor')

      print('Loading model from', model_path_str)
      net = Yolact()

      map_location = None if torch.cuda.is_available() else 'cpu'
      net.load_weights(model_path_str, map_location=map_location)

      net.eval()
      print('Done.')

      if torch.cuda.is_available():
          net = net.cuda()
      
      net.detect.use_fast_nms = True
      cfg.mask_proto_debug = False

  ic = ros_node(net)
  
  srv = ReconfigureServer(YolactConfig, ic.reconfigure_callback)

  rospy.spin()
