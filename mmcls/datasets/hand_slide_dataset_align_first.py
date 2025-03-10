import torch
import random
import numpy as np
from collections import defaultdict

from tqdm import tqdm
import datetime
import sys
import os
import glob
import json
import cv2

from tabulate import tabulate

from .base_dataset import BaseDataset
from .builder import DATASETS

from utils.visualize_hand_pose import vis_hand_pose_3d, create_gif

LABELS = ["none", "up", "down", "left", "right"][:3]
if len(LABELS) == 3:
    print("不考虑左右")
    print("不考虑左右")
    print("不考虑左右")
    print("不考虑左右")
    print("不考虑左右")
    print("不考虑左右") 
    
    
class Frame():
    def __init__(self, labelme_path, landmark, label, num_frame) -> None:
        # 存储单帧的全部信息
        assert labelme_path.endswith('.json'), labelme_path
        assert os.path.exists(labelme_path), labelme_path
        assert label in LABELS, label
        assert isinstance(landmark, np.ndarray), landmark

        self.labelme_path = labelme_path
        self.depth_path = labelme_path.replace(".json", ".png").replace("merge_result", "depth")
        self.landmark = landmark
        self.embedding = self.landmark
        self.num_frame = num_frame
        self.label = label


@DATASETS.register_module()
class HandSlideDatasetAlignFirst(BaseDataset):
    def __init__(self,
                 src_dir,
                 pipeline,
                 duration,
                 num_keypoints,
                 *,
                 single_finger,
                 test_mode):
        assert "backup" not in src_dir, src_dir
        assert "slide/" in src_dir, src_dir
        self.src_dir = src_dir
        
        self.duration = duration
        self.single_finger = single_finger
        self.frames = self.parse_jsons_to_frames(self.src_dir, num_keypoints)

        cache_name = '_'.join(self.src_dir.split("slide/")[-1].split('/')) + ".pkl"
        cache_path = os.path.join("pgs", cache_name)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        # debug模式不加载缓存
        if os.path.exists(cache_path) and not sys.gettrace():
            cache = torch.load(cache_path)
            self.samples = cache["samples"]
            print(f"加载samples缓存 {cache_path} ：生成时间 {cache['modify_time']}")
        else:
            # 无缓存文件，重新生成样本
            self.samples = self.generate_samples()
            print(f"生成缓存到 {cache_path}")
            torch.save({"samples": self.samples,
                        "modify_time": datetime.datetime.now(),
                        "duration": self.duration},
                       cache_path)
        
        if self.single_finger:
            print("使能单指推理")
            for s in self.samples:
                # 保留 [1,6,11,16]
                s["img"][:, 0, :] = 0
                s["img"][:, 2:6, :] = 0
                s["img"][:, 7:11, :] = 0
                s["img"][:, 12:16, :] = 0
        # 统计样本分布
        self.sample_statics = self.static_sample_dist()
        self.visualize_samples()
        
        # 训练阶段进行优质样本挖掘
        if not test_mode:
            self.samples = self.mine_good_cases()
        
        super().__init__(self.src_dir, pipeline)
                    
    def parse_jsons_to_frames(self, src_dir, num_keypoints):
        src_json_paths = sorted(glob.glob(os.path.join(self.src_dir, "**", "merge_result", "*.json"), recursive=True))
        # 新的在最前头
        src_json_paths = src_json_paths[::-1]
        assert len(src_json_paths) != 0, self.src_dir
        # 首先把没有框的样本剔除，保持后续遍历时索引的连续性
        src_json_paths = list(filter(lambda p: "kp" in json.load(open(p, 'r')), src_json_paths))
        
        # 逐个json解析成Frame
        frames = []
        for i, src_json_path in enumerate(src_json_paths):
            json_dict = json.load(open(src_json_path, 'r'))
            # assert "bbox" in json_dict
            assert "kp" in json_dict
            assert "on_table" in json_dict

            # 解析landmark
            landmark = np.zeros((num_keypoints , 3), dtype=np.float32)
            for j in range(num_keypoints):
                landmark[j] = json_dict["kp"][j][:3]
                
            # 确定当前帧的标签
            if json_dict["on_table"]:
                for e in LABELS:
                    if '/' + e in src_json_path:
                        label = e
            else:
                label = "none"
                
            frames.append(Frame(src_json_path, landmark, label, i))
            
        return frames

    def load_annotations(self):
        data_infos = []
        data_infos = self.samples
        return data_infos
        
    # 重写获取标签的函数
    def get_gt_labels(self):
        """Get all ground-truth labels (categories).

        Returns:
            np.ndarray: categories for all images.
        """
        gt_labels = np.array([LABELS.index(data['patch_label']) for data in self.data_infos])
        return gt_labels

    def generate_samples(self):
        '''
        根据帧序列生成样本
        '''
        samples = []
        for i in tqdm(range(len(self.frames) - self.duration + 1)):
            sample = self.generate_single_sample([i, i + self.duration - 1])
            samples.append(sample)
        return samples

    def generate_soft_label(self, prob, label):
        assert prob <= 1, prob
        sl = np.zeros(len(LABELS), dtype=np.float)
        sl[LABELS.index(label)] = prob
        sl[0] = 1 - prob
        return sl
  
    def generate_single_sample(self, indexes):
        # 有效动作首帧对齐
        seq_size = indexes[-1] - indexes[0] + 1
        frames = [self.frames[k] for k in range(indexes[0], indexes[1] + 1)]
        soft_label = np.zeros((seq_size, len(LABELS)))
        cur_label = frames[0].label
        
        if frames[0].label != "none" and frames[1].label == frames[0].label:
            left = indexes[0]
            right = indexes[0]
            while left >= 0 and self.frames[left].label != "none":
                left -= 1
            while right < len(self.frames) and self.frames[right].label != "none":
                right += 1
            left += 1
            right -= 1
        
            for i in range(seq_size):
                # 当前帧是cur_label但是上一帧不是，说明开始了一个新的有效动作，终结当前label
                if i != 0 and (frames[i].label == cur_label and frames[i - 1].label != cur_label):
                    break
                hit_count = sum(frames[k].label == cur_label for k in range(i + 1))
                soft_label[i] = self.generate_soft_label(hit_count / (i + 1), cur_label)
        else:
            soft_label[:, 0] = 1
                    
        result = dict()
        result["src_depth_paths"]  =[f.depth_path for f in frames]
        result["img"] = np.array([f.embedding for f in frames])
        result["gt_label"] = soft_label
        result["per_frame_label"] = [f.label for f in frames]
        result["patch_label"] = frames[0].label

        return result

    def __str__(self):
        dataset_name = '_'.join(self.src_dir.split("slide/")[-1].split('/'))
        s = '\n'.join([dataset_name, self.sample_statics])
        return s

    def static_sample_dist(self):
        print("原始数据统计：")
        begin = 0
        cur_label = "none"
        label_durations = defaultdict(list)
        for i in range(len(self.frames)):
            if i > 0 and self.frames[i - 1].label != self.frames[i].label:
                # 新动作开始
                if self.frames[i].label != "none":
                    begin = i
                    cur_label = self.frames[i].label
                # 动作结束
                else:
                    duration = i - begin
                    label_durations[cur_label].append(duration)

        for k in label_durations:
            label_durations[k] = sorted(label_durations[k])
        
        raw_table = []
        # for k in ["none", "up", "down"]:
        for k in ["up", "down"]:
            row = [k, len(label_durations[k]), sum(label_durations[k]) / len(label_durations[k])]
            raw_table.append(row)
        table = tabulate(
        raw_table,
        headers=["类别", "数量", "平均时长"],
        tablefmt="pipe",
        numalign="left",
        stralign="center",
        )
        print(table)
            
        
        print("数据集训练样本统计：")
        label_durations.clear()
        for s in self.samples:
            m = 0
            while m < len(s["per_frame_label"]) and s["per_frame_label"][m] == s["per_frame_label"][0]:
                m += 1
            label_durations[s["patch_label"]].append(m)
        sample_table = []
        for k in ["none", "up", "down"]:
            row = [k, len(label_durations[k]), sum(label_durations[k]) / len(label_durations[k])]
            sample_table.append(row)
        table = tabulate(
        sample_table,
        headers=["类别", "数量", "平均时长"],
        tablefmt="pipe",
        numalign="left",
        stralign="center",
        )
        print(table)
        
        return table

    def mine_good_cases(self):
        # TODO 静止帧过滤
        none_samples  =[s for s in self.samples if s["patch_label"] == "none"]
        down_samples  =[s for s in self.samples if s["patch_label"] == "down"]
        up_samples  =[s for s in self.samples if s["patch_label"] == "up"]
        sampled_nones = random.sample(none_samples, min(len(none_samples), int((len(up_samples) + len(down_samples)) * 1.5)))
        print(f"滤除不平衡none样本, 保留 {len(sampled_nones)} 个none")
        s = down_samples + up_samples + sampled_nones
        return s

        
    def visualize_samples(self):
        for s in self.samples:
            vis_nps = vis_hand_pose_3d(s["img"], single_finger=True)
            if len(vis_nps) != 0:
                vis_nps = [vis_nps[0]] + vis_nps
                create_gif(vis_nps, "ee.gif")
                print()