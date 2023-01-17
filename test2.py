import matplotlib.pyplot as plt
import random
import tqdm
from concurrent.futures import ProcessPoolExecutor

TRIALS = 1000000
TOSSES = 1000
trials = []

def simulate_trial(tosses):
    trial = []
    for _ in range(tosses):
        trial.append(random.randint(0, 1))
    return trial

if __name__ == "__main__":
    with ProcessPoolExecutor() as executor:
        for trial in tqdm.tqdm(executor.map(simulate_trial, [TOSSES for _ in range(TRIALS)]), total=TRIALS):
            trials.append(trial)
    for i, trial in enumerate(trials):
        print(f"Trial {i + 1}: {sum(trial) / TOSSES}")
    # make a histogram of the results
    plt.hist([sum(trial) / TOSSES for trial in trials])
    plt.savefig("histogram.png")