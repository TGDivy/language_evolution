from framework.experiment_builder import ExperimentBuilder
from framework.utils.arg_extractor import get_args
import numpy as np
import random
import torch
from pettingzoo.mpe import simple_v2, simple_reference_v2, simple_reference_v3

import shutil
import supersuit as ss
from framework.policies.ppo import ppo_policy
from framework.policies.ppo3 import ppo_policy3
from framework.policies.maddpg import maddpg_policy
from framework.policies.ppo_rec import ppo_rec_policy
import os
from torch.utils.tensorboard import SummaryWriter
import warnings

warnings.filterwarnings("ignore")

args = get_args()  # get arguments from command line

# Generate Directories##########################
experiment_name = f"{args.model}-{args.env}-{args.experiment_name}"
experiment_folder = os.path.join(os.path.abspath("experiments"), experiment_name)
experiment_logs = os.path.abspath(os.path.join(experiment_folder, "result_outputs"))
experiment_videos = os.path.abspath(os.path.join(experiment_folder, "videos"))
experiment_saved_models = os.path.abspath(
    os.path.join(experiment_folder, "saved_models")
)

if os.path.exists(experiment_folder):
    shutil.rmtree(experiment_folder)

os.mkdir(experiment_folder)  # create the experiment directory
os.mkdir(experiment_logs)  # create the experiment log directory
os.mkdir(experiment_saved_models)
os.mkdir(experiment_videos)
################################################
logger = SummaryWriter(experiment_logs)

print("\n*****Parameters*****")
space = " "
print(
    "\n".join(
        [
            f"--- {param}: {(20-len(param))*space} {value}"
            for param, value in vars(args).items()
        ]
    )
)
print("*******************")

logger.add_hparams(vars(args), {"rewards/end_reward": 0})

# set seeds
random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.deterministic = args.torch_deterministic
# setup environment ###########################################
if args.env == "simple":
    env = simple_v2
elif args.env == "communication":
    env = simple_reference_v2
elif args.env == "spread":
    env = simple_spread_v2
elif args.env == "adversary":
    env = simple_adversary_v2
env = env.parallel_env()
env = ss.pad_observations_v0(env)
env = ss.pettingzoo_env_to_vec_env_v1(env)
parrallel_env = ss.concat_vec_envs_v1(env, args.num_envs, args.num_envs)
parrallel_env.seed(args.seed)
obs = parrallel_env.reset()
print(
    f"Observation shape: {env.observation_space.shape}, Action space: {parrallel_env.action_space}"
)
args.obs_space = env.observation_space.shape
args.device = "cuda"
##############################################################

############### MODEL ########################################
if args.model == "ppo":
    Policy = ppo_policy
elif args.model == "ppo-rec":
    Policy = ppo_rec_policy
elif args.model == "maddpg":
    Policy = maddpg_policy
elif args.model == "ppo_policy3":
    Policy = ppo_policy3
else:
    pass
Policy = Policy(args, logger)
###############################################################

exp = ExperimentBuilder(
    train_environment=parrallel_env,
    test_environment=env,
    Policy=Policy,
    experiment_name=experiment_name,
    logfolder=experiment_videos,
    videofolder=experiment_videos,
    episode_len=args.episode_len,
    logger=logger,
)
exp.run_experiment()
