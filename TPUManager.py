import ray_tpu
import ray
import threading


class TPUManager:
    def __init__(self, num_tpus, address):
        self.num_tpus = num_tpus
        self.tpus = ["ray-tpu-" + str(i) for i in range(1, num_tpus + 1)]
        self.address = address

    def setup(self):
        for tpu in self.tpus:
            ray_tpu.create_tpu(tpu, "us-central1-f", "v2-8", True)
        for tpu in self.tpus:
            ray_tpu.wait_til(tpu, 'us-central1-f', {"state": "READY"})
        threads = []
        for tpu in self.tpus:
            threads.append(threading.Thread(target=ray_tpu.start_ray, args=(tpu, "us-central1-f", self.address)))
            threads[-1].start()
        for thread in threads:
            thread.join()
        return True
