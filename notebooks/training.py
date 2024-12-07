# %%
import argparse
from pathlib import Path

import torch
from snake.envs import SnakeGrid
from snake.render.asciinema import render_trajectory
from tensordict.nn import TensorDictModule as Mod
from tensordict.nn import TensorDictSequential as Seq
from torchrl.envs import (
    FlattenObservation,
    GymEnv,
    StepCounter,
    TransformedEnv,
    UnsqueezeTransform,
)
from torchrl.modules import (
    MLP,
    Actor,
    ConvNet,
    DdpgCnnActor,
    DdpgCnnQNet,
    DuelingCnnDQNet,
    EGreedyModule,
    QValueActor,
)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.set_default_device(device)

# %%
parser = argparse.ArgumentParser()
parser.add_argument("exp_name")
parser.add_argument(
    "--offline",
    "-o",
    action="store_true",
    help="Stores the run locally with the option to sync it to wandb after the fact.",
)
parser.add_argument(
    "--tags",
    "-t",
    nargs="*",
    default=[],
    help=(
        "Tags to add to the experiment in wandb. Can also be added in post through the"
        " web UI."
    ),
)
parser.add_argument(
    "--gamma",
    "-g",
    default=0.99,
    type=float,
    help="Discount factor to use in the return/value function calculations.",
)
parser.add_argument("--board-size", "-s", default=5, help="Playable board size.")
parser.add_argument(
    "--max-episode-steps",
    "-m",
    default=5000,
    type=int,
    help="Maximum steps allowed in an episode before it is truncated.",
)
parser.add_argument(
    "--adam-learning-rate",
    "-L",
    default=0.002,
    type=float,
    help="The learning rate to use in the ADAM optimizer.",
)
parser.add_argument(
    "--kernel-sizes",
    "-k",
    nargs=2,
    type=int,
    default=[3, 2],
    help="Kernel sizes to use in the CNN layers.",
)
parser.add_argument(
    "--buffer-length",
    "-l",
    default=1_000_000,
    type=int,
    help="Length of the ReplayBuffer",
)
parser.add_argument(
    "--epsilon-bounds",
    "-e",
    nargs=2,
    type=float,
    default=[0.7, 0.00],
    help=(
        "Start and end values for the epsilon to use in the epsilon-greedy exploration"
        " module. Controls exploration vs. exploitation during training. Pass the high"
        " (starting) value first followed by the low (end) value."
    ),
)
parser.add_argument(
    "--init-rand-steps",
    "-r",
    default=5000,
    type=int,
    help=(
        "How many random steps should be taken before engaging the exploration module."
    ),
)
parser.add_argument(
    "--steps-per-batch",
    "-b",
    default=100,
    type=int,
    help="How many steps to take in each batch from the data collector.",
)
parser.add_argument(
    "--optim-steps",
    "-S",
    default=10,
    type=int,
    help="How many passes of the optimizer to make during each batch sample",
)

args = parser.parse_args()

# %%
env = GymEnv("snake/SnakeGrid", size=args.board_size)
env = TransformedEnv(env, UnsqueezeTransform(-3, in_keys=["observation"]))
env = TransformedEnv(env, StepCounter(max_steps=args.max_episode_steps))
env.auto_register_info_dict()

# %%
env.reset()

# %%
# value_net = ConvNet(strides=1, kernel_sizes=[3, 3, 2])
# policy = Seq(QValueActor(value_net, spec=env.action_spec))
# value_net = DdpgCnnQNet()
# policy = Actor(
#     DdpgCnnActor(
#         action_dim=env.action_spec.shape[-1], conv_net_kwargs={"kernel_sizes": 2}
#     )
# )
value_net = DuelingCnnDQNet(
    out_features=env.action_spec.shape[-1],
    out_features_value=1,
    cnn_kwargs={
        "kernel_sizes": args.kernel_sizes,
        "strides": 1,
        "depth": 2,
        "num_cells": [32, 64],
    },
    mlp_kwargs={
        "depth": 2,
        "num_cells": [
            64,
            64,
        ],
    },
    device=device,
)
policy = QValueActor(value_net, spec=env.action_spec)
policy(env.fake_tensordict())


rollout = env.rollout(max_steps=5, policy=policy)

# %%
exploration_module = EGreedyModule(
    env.action_spec,
    annealing_num_steps=args.buffer_length * args.optim_steps,
    eps_init=args.epsilon_bounds[0],
    eps_end=args.epsilon_bounds[1],
)

policy_explore = Seq(policy, exploration_module)

# %%
from torchrl.collectors import SyncDataCollector
from torchrl.data import LazyTensorStorage, ReplayBuffer

collector = SyncDataCollector(
    env,
    policy_explore,
    frames_per_batch=args.steps_per_batch,
    total_frames=-1,
    init_random_frames=args.init_rand_steps,
    device=device,
)
rb = ReplayBuffer(storage=LazyTensorStorage(args.buffer_length, device=device))

from torch.optim import Adam

# %%
from torchrl.objectives import DQNLoss, SoftUpdate

loss = DQNLoss(value_network=policy, action_space=env.action_spec, delay_value=True)
loss.make_value_estimator(gamma=args.gamma)
optim = Adam(loss.parameters(), lr=args.adam_learning_rate)
updater = SoftUpdate(loss, eps=0.99)

import time

# %%
from torchrl._utils import logger as torchrl_logger

path = Path("./output")

try:
    print(
        "Attempting to integrate with wandb; dismiss the following messages if you have"
        " not set it up."
    )
    from torchrl.record import WandbLogger

    logger = WandbLogger(
        project="rlsnakes",
        exp_name=args.exp_name,
        offline=args.offline,
        tags=["duelingdqn"] + args.tags,
        config=args,
    )
except Exception:
    print("wandb unavailable; falling back to CSV logging.")
    from torchrl.record import CSVLogger

    logger = CSVLogger(exp_name=args.exp_name, log_dir=str(path))
    logger.log_hparams(vars(args))

# %%
total_count = 0
total_episodes = 0
t0 = time.time()
prev_max = 0
for i, data in enumerate(collector):
    # Write data in replay buffer
    rb.extend(data)

    max_step_count = rb[:]["next", "step_count"].max()
    max_snake_length = rb[:]["next", "snake_length"].max()

    if max_snake_length > prev_max and max_snake_length > 5:
        i_max = rb["next", "snake_length"].argmax()
        i_start = rb["next", "done"][:i_max].argwhere()[-1][0] + 1
        i_end = i_max + rb["next", "done"][i_max:].argwhere()
        if i_end.numel == 0:
            # We haven't gotten to the end of this trajectory yet; keep going
            # until we do
            continue

        i_end = i_end[0, 0]

        if rb["next", "truncated"][i_end].item():
            print(
                "Warning: the trajectory which yielded a max score of"
                f" {max_snake_length} was truncated at {args.max_episode_steps} steps."
            )

        print(
            f"New max of {max_snake_length}; rb = {len(rb):,}/{args.buffer_length:,};"
            f" eps = {exploration_module.eps}"
        )
        prev_max = max_snake_length

        trajectory = rb["next"][i_start : i_end + 1]

        video_dir = path / args.exp_name / "videos"

        video_dir.mkdir(parents=True, exist_ok=True)

        render_trajectory(
            str(video_dir / f"snake_length_{max_snake_length}.cast"),
            SnakeGrid._render_as_string,
            tensordict=trajectory.cpu(),
        )

        # logger.log_video(f"max_snake_length_{max_snake_length}", )

    if len(rb) > args.init_rand_steps:
        # Optim loop (we do several optim steps
        # per batch collected for efficiency)
        for _ in range(args.optim_steps):
            sample = rb.sample(128)
            loss_vals = loss(sample)
            loss_vals["loss"].backward()
            optim.step()
            optim.zero_grad()
            # Update exploration factor
            # In theory, stepping by 100 each time and rb
            exploration_module.step(data.numel())
            # Update target params
            updater.step()
            if i % 10000:
                logger.log_scalar("max_score", max_snake_length)
                logger.log_scalar("max_steps", max_step_count)
                logger.log_scalar("total_count", total_count)
                logger.log_scalar("total_episodes", total_episodes)
                logger.log_scalar("epsilon", exploration_module.eps)

            total_count += data.numel()
            total_episodes += data["next", "done"].sum()
    if max_snake_length == 100:
        break

t1 = time.time()

torchrl_logger.info(
    f"solved after {total_count} steps, {total_episodes} episodes and in {t1-t0}s."
)
