from tqdm import tqdm
import numpy as np
from datasets import load_dataset

from functools import partial
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import io
import urllib

import PIL.Image

from datasets import load_dataset
from datasets.utils.file_utils import get_datasets_user_agent

import ray

USER_AGENT = get_datasets_user_agent()


def fetch_single_image(image_url, timeout=None, retries=0):
    for _ in range(retries + 1):
        try:
            request = urllib.request.Request(
                image_url,
                data=None,
                headers={"user-agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=timeout) as req:
                image = PIL.Image.open(io.BytesIO(req.read())).resize(
                    (64, 64))
                # convert to array H, W, C
                image = np.array(image)[..., :3] / 127.5 - 1.0

            break
        except Exception:
            image = None
    return image


def fetch_images(batch, num_threads, timeout=None, retries=0):
    fetch_single_image_with_args = partial(
        fetch_single_image, timeout=timeout, retries=retries)
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        batch["image"] = list(executor.map(
            fetch_single_image_with_args, batch["image_url"]))
    return batch


@ray.remote
class SharedStorage:
    def __init__(self):
        self.images = []
        self.texts = []

    def add_data(self, images, texts):
        self.images.extend(images)
        self.texts.extend(texts)

    def get_batch(self, batch_size):
        if len(self.images) < batch_size:
            return None
        images = []
        texts = []
        for _ in range(batch_size):
            images.append(self.images.pop(0))
            texts.append(self.texts.pop(0))
        images = np.array(images)
        return images, texts

    

@ray.remote(num_cpus=5)
class DatasetFetcher:
    def __init__(self):
        dataset = load_dataset("red_caps", split="train")
        dataset = dataset.remove_columns("created_utc")
        dataset = dataset.remove_columns("crosspost_parents")
        dataset = dataset.remove_columns("author")
        dataset = dataset.remove_columns("subreddit")
        dataset = dataset.remove_columns("score")
        self.dataset = dataset

    def get_data(self):
        return self.dataset[np.random.randint(len(self.dataset))]


@ray.remote
class DataCollector:
    def __init__(self, shared_storage, dataset):
        self.shared_storage = shared_storage
        self.dataset = dataset

    def collect(self):
        while True:
            item = self.dataset.get_data.remote()
            item = ray.get(item)
            image = fetch_single_image(item["image_url"])
            if image is None:
                continue
            image = np.array(image, dtype=np.float32)
            if image.shape != (64, 64, 3):
                continue
            self.shared_storage.add_data.remote([image], [item["caption"]])

@ray.remote(num_cpus=2)
class DataManager:
    def __init__(self, num_workers, batch_size):
        self.shared_storage = SharedStorage.remote()
        self.batch_size = batch_size
        self.datasetFetcher = DatasetFetcher.remote()
        self.workers = [DataCollector.remote(self.shared_storage, self.datasetFetcher) for _ in range(num_workers)]
        
    def start(self):
        for worker in self.workers:
            worker.collect.remote()

    def get_batch(self):
        data = None
        while data is None:
            data = ray.get(self.shared_storage.get_batch.remote(self.batch_size))
        return data