# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

# pylint: disable=missing-module-docstring
# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring


import copy
from math import inf
import tempfile
from typing import Type, cast
import unittest

import numpy as np
import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP

import fairscale.optim as optim
from fairscale.utils.testing import skip_if_no_cuda, skip_if_single_gpu

BACKEND = dist.Backend.NCCL if torch.cuda.is_available() else dist.Backend.GLOO  # type: ignore
DEVICE = "cuda" if torch.cuda.is_available() else torch.device("cpu")

try:
    from torch.distributed import broadcast_object_list  # noqa

    _torch_broadcast_object = True
except ImportError:
    from fairscale.optim.utils import broadcast_object  # noqa

    _torch_broadcast_object = False


def dist_init(rank, world_size, tempfile_name, backend=BACKEND):
    url = "file://" + tempfile_name
    dist.init_process_group(init_method=url, backend=backend, rank=rank, world_size=world_size)


class TestSingleRank(unittest.TestCase):
    """
    All the following tests do not check for inter-process communication
    """

    def setUp(self):
        dist_init(0, 1, tempfile.mkstemp()[1])

    def tearDown(self):
        torch.distributed.destroy_process_group()

    def test_create(self):
        params = [torch.rand(1)]
        o = optim.OSS(params, lr=0.01)

    def test_state_dict(self):
        x = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        o = optim.OSS([x], lr=0.1, momentum=0.9)
        x.backward()
        o.step()
        assert x == torch.tensor([0.9], device=DEVICE)
        assert o.optim.state[x]["momentum_buffer"] == torch.tensor([1.0], device=DEVICE)
        o.zero_grad()
        o.consolidate_state_dict()  # Sync state dict in between replicas - even if there are none
        state_dict = o.state_dict()

        # Check that the state dict is pytorch-compliant key wise
        assert "param_groups" in state_dict.keys()
        assert "state" in state_dict.keys()

        # Check that the pulled state is what we expect, and that we have all the expected keys
        assert state_dict["param_groups"][0]["lr"] == 0.1
        assert state_dict["param_groups"][0]["momentum"] == 0.9
        assert not state_dict["param_groups"][0]["nesterov"]
        assert state_dict["param_groups"][0]["weight_decay"] == 0.0
        assert state_dict["param_groups"][0]["dampening"] == 0.0

        # Check that the pulled state and the .param_groups attribute are in sync
        for k in state_dict["param_groups"][0].keys():
            if k != "params":
                assert state_dict["param_groups"][0][k] == o.param_groups[0][k]

        # Check that it's correctly loaded
        o = optim.OSS([x], lr=0.01)
        o.load_state_dict(state_dict)
        # Check that state is correct and on proper device
        assert o.optim.state[x]["momentum_buffer"] == torch.tensor([1.0], device=DEVICE)

        # We should now be using a lr of 0.1, both within the optimizer
        # and as exposed by the .param_groups attribute
        assert o.param_groups[0]["lr"] == 0.1
        x.backward()
        o.step()
        assert x == torch.tensor([0.71], device=DEVICE)
        assert o.optim.state[x]["momentum_buffer"] == torch.tensor([1.9], device=DEVICE)

        # Check that the exposed param_groups are on the proper device
        assert o.param_groups[0]["params"][0].device == x.device

    def test_lr_scheduler(self):
        x = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        x2 = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        o = optim.OSS([x], lr=0.01)
        o2 = torch.optim.SGD([x2], lr=0.01)
        s = torch.optim.lr_scheduler.StepLR(o, 1)
        s2 = torch.optim.lr_scheduler.StepLR(o2, 1)
        for _ in range(5):
            x.backward()
            o.zero_grad()
            o.step()
            s.step()
            x2.backward()
            o2.zero_grad()
            o2.step()
            s2.step()
            assert x == x2

    def test_step_with_kwargs(self):
        class SGDWithStepKWArg(torch.optim.SGD):
            def step(self, closure=None, kwarg=[]):
                super().step()
                kwarg.append(5)

        kwarg = []
        x = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        o = optim.OSS([x], SGDWithStepKWArg, lr=0.1)
        x.backward()
        o.step(0, kwarg=kwarg)
        assert kwarg == [5]
        assert x == torch.tensor([0.9], device=DEVICE)

    def test_step_with_extra_inner_key(self):
        class SGDWithNewKey(torch.optim.SGD):
            # Dummy optimizer which adds a new key to the param groups
            def step(self, closure=None):
                super().step()
                self.param_groups[0]["new_key"] = 0.1

        x = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        o = optim.OSS([x], SGDWithNewKey, lr=0.1)
        x.backward()
        o.step()
        assert o.param_groups[0]["new_key"] == 0.1
        assert x == torch.tensor([0.9], device=DEVICE)

    def test_step_without_closure(self):
        class SGDWithoutClosure(torch.optim.SGD):
            def step(self):
                return super().step()

        x = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        o = optim.OSS([x], SGDWithoutClosure, lr=0.1)
        x.backward()
        o.step()
        assert x == torch.tensor([0.9], device=DEVICE)

    def test_implicit_local_state_dict(self):
        x = torch.tensor([1.0], device=DEVICE, requires_grad=True)
        o = optim.OSS([x], lr=0.1)
        local_state_dict = o.state_dict()
        o = optim.OSS([x], lr=0.01)
        o.load_state_dict(local_state_dict)
        # We should now be using a lr of 0.1.
        assert o.optim.param_groups[0]["lr"] == 0.1
        assert o.param_groups[0]["lr"] == 0.1
        x.backward()
        o.step()
        assert x == torch.tensor([0.9], device=DEVICE)


def run_test_add_param_group(rank, world_size, tempfile_name):
    dist_init(rank, world_size, tempfile_name)

    # Test with all parameters trainable to begin with
    def all_trainable():
        params = []
        for size in [4, 5, 2, 6, 4]:
            params.append(torch.rand(size, 1))

        # Make sure that the params are trainable, enforces size-based partitioning
        for p in params:
            p.requires_grad = True

        o = optim.OSS(params, lr=0.1)

        assert len(o.param_groups) == 1
        o.add_param_group({"params": [torch.rand(3, 1)]})

        assert len(o.param_groups) == 2
        # Verify that added group is added to the correct partition making all have 8 elements.
        assert sum([x.numel() for g in o.optim.param_groups for x in g["params"]]) == 8
        assert len(o.optim.param_groups) == 2

    # Test a pathological config with a first big non-trainable param
    def some_trainable():
        params = []
        for size in [100, 3, 5, 2, 6, 4]:
            params.append(torch.rand(size, 1))

        # Make sure that the params are trainable, enforces size-based partitioning
        for p in params[1:]:
            p.requires_grad = True

        o = optim.OSS(params, lr=0.1)

        assert len(o.param_groups) == 1
        o.add_param_group({"params": [torch.rand(3, 1)]})

        assert len(o.param_groups) == 2
        assert len(o.optim.param_groups) == 2

    all_trainable()
    some_trainable()

    dist.destroy_process_group()


def test_add_param_group():
    world_size = 3
    if not torch.cuda.is_available() or torch.cuda.device_count() < world_size:
        pytest.skip("Not enough GPUs for NCCL-based test")
    temp_file_name = tempfile.mkstemp()[1]
    mp.spawn(run_test_add_param_group, args=(world_size, temp_file_name), nprocs=world_size, join=True)


def run_test_zero_grad(rank, world_size, tempfile_name):
    dist_init(rank, world_size, tempfile_name)
    x = torch.rand(1)
    m = torch.nn.Linear(1, 1)
    o = optim.OSS(m.parameters(), lr=0.1)
    y = m(x)
    y.backward(x)
    assert m.weight.grad
    assert m.bias.grad
    o.zero_grad()
    assert not m.weight.grad
    assert not m.bias.grad

    dist.destroy_process_group()


def test_zero_grad():
    world_size = 2
    temp_file_name = tempfile.mkstemp()[1]
    mp.spawn(run_test_zero_grad, args=(world_size, temp_file_name), nprocs=world_size, join=True)


def run_test_step(rank, world_size, tempfile_name):
    dist_init(rank, world_size, tempfile_name, backend="gloo")
    x = torch.tensor([float(rank + 1)], device=rank)
    m = torch.nn.Linear(1, 1)
    m.weight.data = torch.tensor([[1.0]])
    m.bias.data = torch.tensor([2.0])
    m.to(rank)
    o = optim.OSS(m.parameters(), lr=0.1)
    y = m(x)
    y.backward(x)
    for p in m.parameters():
        dist.all_reduce(p.grad.data, op=dist.ReduceOp.SUM)
        p.grad.data /= world_size
    o.step()
    assert m.weight == torch.tensor([[0.75]], device=rank)
    assert m.bias == torch.tensor([1.85], device=rank)

    dist.destroy_process_group()


@skip_if_single_gpu
def test_step():
    world_size = 2
    temp_file_name = tempfile.mkstemp()[1]

    mp.spawn(run_test_step, args=(world_size, temp_file_name), nprocs=world_size, join=True)


def run_test_step_with_closure(rank, world_size, tempfile_name, optimizer=None):
    dist_init(rank, world_size, tempfile_name)

    x_val = rank + 1
    weight = 1.0
    bias = 2.0
    error = 1.0
    target = torch.tensor([x_val * weight + bias + error], device=rank)
    loss_fn = torch.nn.L1Loss()

    x = torch.tensor([float(x_val)], device=rank)
    m = torch.nn.Linear(1, 1)
    m.weight.data = torch.tensor([[weight]])
    m.bias.data = torch.tensor([bias])
    m.to(rank)

    o = optim.OSS(m.parameters(), lr=0.1)

    y = m(x)
    y.backward(x)
    for p in m.parameters():
        dist.all_reduce(p.grad.data, op=dist.ReduceOp.SUM)
        p.grad.data /= world_size

    def closure():
        o.zero_grad()
        output = m(x)
        loss = loss_fn(output, target)
        loss.backward()
        return loss

    loss = o.step(closure=closure)

    assert loss == torch.tensor(error, device=rank)
    assert m.weight == torch.tensor([[1.1]], device=rank)
    assert m.bias == torch.tensor([2.1], device=rank)

    dist.destroy_process_group()


@skip_if_no_cuda
def test_step_with_closure():
    world_size = min(2, torch.cuda.device_count())
    temp_file_name = tempfile.mkstemp()[1]

    mp.spawn(run_test_step_with_closure, args=(world_size, temp_file_name), nprocs=world_size, join=True)


def run_test_sharding(rank, world_size, tempfile_name):
    dist_init(rank, world_size, tempfile_name)
    params = []
    for size in [5, 4, 2, 6, 4, 3]:
        params.append(torch.rand(size, 1))

    # Make sure that the params are trainable, enforces size-based partitioning
    for p in params:
        p.requires_grad = True

    o = optim.OSS(params, lr=0.1)
    assert sum([x.numel() for x in o.optim.param_groups[0]["params"]]) == 8

    dist.destroy_process_group()


def test_sharding():
    world_size = 3
    if not torch.cuda.is_available() or torch.cuda.device_count() < world_size:
        pytest.skip("Not enough GPUs for NCCL-based test")
    temp_file_name = tempfile.mkstemp()[1]

    mp.spawn(run_test_sharding, args=(world_size, temp_file_name), nprocs=world_size, join=True)


def run_test_collect_shards(rank, world_size, reference_rank, tempfile_name):
    dist_init(rank, world_size, tempfile_name)
    device = torch.device(rank) if torch.cuda.device_count() > 1 else DEVICE

    # Run a dummy step so that the optimizer state dict exists
    batch, input_width, hidden, target_width = 3, 3, 3, 5
    target = torch.rand((batch, target_width), device=device)
    inputs = torch.rand((batch, input_width), device=device)

    model = torch.nn.Sequential(torch.nn.Linear(input_width, hidden), torch.nn.Linear(hidden, target_width))
    model.to(device)

    loss_fn = torch.nn.L1Loss()
    loss_fn.to(device)

    # With SGD, Momentum is required to get a state to shard
    optimizer = optim.OSS(model.parameters(), lr=0.1, momentum=0.99)

    def closure():
        optimizer.zero_grad()
        output = model(inputs)
        loss = loss_fn(output, target)
        loss.backward()
        return loss

    _ = optimizer.step(closure=closure)

    # Update the optimizer state on the reference rank
    optimizer.consolidate_state_dict(recipient_rank=reference_rank)

    # Fetch the state on the reference rank
    # - check that it has the correct size
    # - load it again
    if rank == reference_rank:
        optimizer_state_dict = optimizer.state_dict()
        assert len(optimizer_state_dict["state"]) == len(list(model.parameters()))
    else:
        optimizer_state_dict = {}

    optim_state = [optimizer_state_dict]
    if _torch_broadcast_object:
        dist.broadcast_object_list(optim_state, src=reference_rank, group=dist.group.WORLD)
        optimizer_state_dict = optim_state[0]
    else:
        optimizer_state_dict = optim.utils.broadcast_object(
            optimizer_state_dict, src_rank=reference_rank, group=dist.group.WORLD, dist_device=device
        )

    # Load the optimizer state dict
    optimizer.load_state_dict(optimizer_state_dict)
    dist.destroy_process_group()


def test_collect_shards():
    world_size = 3
    temp_file_name = tempfile.mkstemp()[1]

    if torch.cuda.is_available():
        world_size = min(world_size, torch.cuda.device_count())
    reference_rank = 0

    mp.spawn(
        run_test_collect_shards, args=(world_size, reference_rank, temp_file_name), nprocs=world_size, join=True,
    )


def run_test_reproducibility(rank, world_size, reference_rank, tempfile_name):
    dist_init(rank, world_size, tempfile_name)
    device = torch.device(rank) if torch.cuda.device_count() > 1 else DEVICE

    # Run a dummy step so that the optimizer state dict exists
    batch, input_width, hidden, target_width = 3, 3, 3, 5
    target = torch.rand((batch, target_width), device=device)
    inputs = torch.rand((batch, input_width), device=device)

    model = torch.nn.Sequential(torch.nn.Linear(input_width, hidden), torch.nn.Linear(hidden, target_width))
    model.to(device)

    loss_fn = torch.nn.L1Loss()
    loss_fn.to(device)

    optimizer = optim.OSS(model.parameters(), optim=torch.optim.RMSprop, lr=0.1)

    def closure():
        optimizer.zero_grad()
        output = model(inputs)
        loss = loss_fn(output, target)
        loss.backward()
        return loss

    _ = optimizer.step(closure=closure)

    # Update the optimizer state on the reference rank
    optimizer.consolidate_state_dict(recipient_rank=reference_rank)

    # Fetch the state on the reference rank, broadcast to the other ones
    if rank == reference_rank:
        optimizer_state_dict = optimizer.state_dict()
    else:
        optimizer_state_dict = {}

    optim_state = [optimizer_state_dict]
    if _torch_broadcast_object:
        dist.broadcast_object_list(optim_state, src=reference_rank, group=dist.group.WORLD)
        optimizer_state_dict = optim_state[0]
    else:
        optimizer_state_dict = optim.utils.broadcast_object(
            optimizer_state_dict, src_rank=reference_rank, group=dist.group.WORLD, dist_device=device
        )

    # Run two steps, log the loss
    _ = optimizer.step(closure=closure)
    reference_loss = optimizer.step(closure=closure)

    # Load the optimizer state dict, rewind the state two steps back
    optimizer.load_state_dict(optimizer_state_dict)

    # Run two new steps, log the loss again and check that we get the same
    _ = optimizer.step(closure=closure)
    test_loss = optimizer.step(closure=closure)

    assert torch.allclose(reference_loss, test_loss)

    dist.destroy_process_group()


def test_reproducibility():
    world_size = 2
    temp_file_name = tempfile.mkstemp()[1]

    if torch.cuda.is_available() and torch.cuda.device_count() < world_size:
        # Bail out if not enough devices
        return

    reference_rank = 0

    mp.spawn(
        run_test_collect_shards, args=(world_size, reference_rank, temp_file_name), nprocs=world_size, join=True,
    )


def run_test_multiple_groups(rank, world_size, tempfile_name):
    # Only work with the even ranks, to check that the global_rank indexing is properly used
    dist_init(rank=rank, world_size=world_size, tempfile_name=tempfile_name, backend="gloo")
    sub_group_ranks = [0, 2, 4]
    process_group = torch.distributed.new_group(ranks=sub_group_ranks, backend="gloo")

    # Make sure that all the ranks get different training data
    # So that the sync check in between their models is meaningful
    torch.manual_seed(rank)
    np.random.seed(rank)

    # Standard deep learning setup
    device = "cpu"
    epochs, batch, input_width, hidden, target_width = 5, 3, 20, 10, 5
    loss_fn = torch.nn.L1Loss().to(device)

    def check(optimizer):
        # Just run a couple of epochs, check that the model is properly updated
        for _ in range(epochs):
            target = torch.rand((batch, target_width), device=device)
            inputs = torch.rand((batch, input_width), device=device)

            def closure():
                optimizer.zero_grad()
                output = model(inputs)
                loss = loss_fn(output, target)
                loss /= world_size
                loss.backward()
                dist.all_reduce(loss, group=process_group)  # Not strictly needed for the test below

                return loss

            _ = optimizer.step(closure=closure)

            # Check that all the params are the same on all ranks
            for pg in optimizer.param_groups:
                for p in pg["params"]:
                    receptacle = [p.clone() for _ in sub_group_ranks] if rank == 0 else []
                    dist.gather(p, receptacle, dst=0, group=process_group)
                    if rank == 0:
                        for sync_p in receptacle[1:]:
                            assert torch.all(torch.eq(receptacle[0], sync_p)), "Models differ in between ranks"

    if rank in sub_group_ranks:
        # Model fitting in the broadcast bucket
        model = torch.nn.Sequential(torch.nn.Linear(input_width, hidden), torch.nn.Linear(hidden, target_width)).to(
            device
        )

        # With SGD, Momentum is required to get a state to shard
        optimizer = optim.OSS(
            model.parameters(), lr=0.1, momentum=0.99, group=process_group, broadcast_buffer_size=2 ** 20
        )
        check(optimizer)

        # Model not-fitting in the broadcast bucket
        model = torch.nn.Sequential(torch.nn.Linear(input_width, hidden), torch.nn.Linear(hidden, target_width)).to(
            device
        )

        # With SGD, Momentum is required to get a state to shard
        optimizer = optim.OSS(model.parameters(), lr=0.1, momentum=0.99, group=process_group, broadcast_buffer_size=0)
        check(optimizer)

    dist.destroy_process_group(process_group)
    dist.destroy_process_group()


def test_multiple_groups():
    world_size = 6
    temp_file_name = tempfile.mkstemp()[1]

    mp.spawn(
        run_test_multiple_groups, args=(world_size, temp_file_name), nprocs=world_size, join=True,
    )


def run_gradient_clipping(rank, world_size, tempfile_name):
    dist_init(rank, world_size, tempfile_name, backend="gloo")
    device = torch.device(rank)
    torch.manual_seed(rank)  # make sure that the different rank get different data

    # Run a dummy step so that the optimizer state dict exists
    batch, input_width, hidden, target_width = 3, 20, 10, 5
    target = torch.rand((batch, target_width), device=device)
    inputs = torch.rand((batch, input_width), device=device)
    NORMS = [1.0, 2.0, 1, 2, inf]
    CLIP_NORM = 0.3

    def check(norm):
        model_oss = torch.nn.Sequential(
            torch.nn.Linear(input_width, hidden),
            torch.nn.Linear(hidden, hidden),
            torch.nn.Linear(hidden, target_width),
        ).to(device)
        model = copy.deepcopy(model_oss)

        # For this test the gradients are (all) reduced in the same way in between the torch reference and fairscale.
        # Normally OSS would use ShardedDDP and only reduce to the proper rank, but this does not change the
        # gradient norm computation from OSS and adds a dependency.
        # to keep the comparison apples-to-apples DDP is used in both cases
        model_oss = DDP(module=model_oss, device_ids=[rank],)
        sharded_optimizer = optim.OSS(model_oss.parameters(), lr=0.1, momentum=0.99)

        model = DDP(model, device_ids=[rank],)

        loss_fn = torch.nn.L1Loss()
        loss_fn.to(device)

        model.zero_grad()
        model_oss.zero_grad()

        outputs = model(inputs)
        outputs_oss = model_oss(inputs)

        loss = loss_fn(outputs, target)
        loss.backward()

        loss_oss = loss_fn(outputs_oss, target)
        loss_oss.backward()

        # Check the equivalence with the non-sharded optim
        oss_total_norm = sharded_optimizer.clip_grad_norm(CLIP_NORM, norm_type=norm)
        total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), CLIP_NORM, norm_type=norm)
        assert torch.allclose(oss_total_norm, total_norm), "torch and fairscale should return the same grad norm"

        # Check that the params have indeed been clipped
        for params in sharded_optimizer.per_device_params.values():
            for param in filter(lambda x: x.grad is not None, params[rank]):
                assert torch.norm(param.grad, p=norm) < CLIP_NORM, f"param grad norm above clip : {param.grad}"

    for norm in NORMS:
        print(f"Checking norm {norm}")
        check(norm)

    dist.destroy_process_group()


@skip_if_no_cuda
def test_gradient_clipping():
    world_size = 3
    temp_file_name = tempfile.mkstemp()[1]

    if torch.cuda.is_available():
        world_size = min(world_size, torch.cuda.device_count())
    reference_rank = 0

    mp.spawn(
        run_gradient_clipping, args=(world_size, temp_file_name), nprocs=world_size, join=True,
    )


def run_state_dict_distributed(rank, world_size, tempfile_name):
    dist_init(rank, world_size, tempfile_name, backend="gloo")
    device = torch.device(rank)
    torch.manual_seed(rank)  # make sure that the different rank get different data

    # Run a dummy step so that the optimizer state dict exists
    batch, input_width, hidden, target_width = 3, 20, 10, 5
    target = torch.rand((batch, target_width), device=device)
    inputs = torch.rand((batch, input_width), device=device)

    model_oss1 = torch.nn.Sequential(
        torch.nn.Linear(input_width, hidden), torch.nn.Linear(hidden, hidden), torch.nn.Linear(hidden, target_width),
    ).to(device)
    model_oss2 = copy.deepcopy(model_oss1)

    # For this test the gradients are (all) reduced in the same way in between the torch reference and fairscale.
    # Normally OSS would use ShardedDDP and only reduce to the proper rank, but this does not change the
    # gradient norm computation from OSS and adds a dependency.
    # to keep the comparison apples-to-apples DDP is used in both cases
    model_oss1 = DDP(module=model_oss1, device_ids=[rank],)
    sharded_optimizer1 = optim.OSS(model_oss1.parameters(), lr=0.1, momentum=0.99)
    model_oss2 = DDP(module=model_oss2, device_ids=[rank],)
    sharded_optimizer2 = optim.OSS(model_oss2.parameters(), lr=0.1, momentum=0.99)

    def run_grad_step(device, model, optimizer):
        loss_fn = torch.nn.L1Loss()
        loss_fn.to(device)

        model.zero_grad()

        outputs = model(inputs)

        loss = loss_fn(outputs, target)
        loss.backward()

        optimizer.step()
        optimizer.zero_grad()

    # save and reload without taking any steps
    sharded_optimizer2.consolidate_state_dict()
    state_dict2 = sharded_optimizer2.state_dict()
    sharded_optimizer2 = optim.OSS(model_oss2.parameters(), lr=0.1, momentum=0.99)
    sharded_optimizer2.load_state_dict(state_dict2)

    # now take a step and check that parameters are equal
    # take a step
    run_grad_step(device, model_oss1, sharded_optimizer1)
    run_grad_step(device, model_oss2, sharded_optimizer2)

    # check that model parameters are equal
    for param1, param2 in zip(model_oss1.parameters(), model_oss2.parameters()):
        assert torch.allclose(param1, param2), "parameters of the two identical models have diverged (before any steps)"

    # take a step
    run_grad_step(device, model_oss1, sharded_optimizer1)
    run_grad_step(device, model_oss2, sharded_optimizer2)

    # check that model parameters are equal
    for param1, param2 in zip(model_oss1.parameters(), model_oss2.parameters()):
        assert torch.allclose(param1, param2), "parameters of the two identical models have diverged (before saving)"

    # save the state dict for one model only
    sharded_optimizer2.consolidate_state_dict()
    state_dict2 = sharded_optimizer2.state_dict()

    # Check that the pulled state and the .param_groups attribute are in sync
    for replica in range(len(state_dict2["param_groups"])):
        for k in state_dict2["param_groups"][replica].keys():
            if k != "params":
                assert state_dict2["param_groups"][replica][k] == sharded_optimizer2.param_groups[0][k]

    # take a step
    run_grad_step(device, model_oss1, sharded_optimizer1)
    run_grad_step(device, model_oss2, sharded_optimizer2)

    # check that saving did not cause a change in the parameters
    for param1, param2 in zip(model_oss1.parameters(), model_oss2.parameters()):
        assert torch.allclose(
            param1, param2
        ), "parameters of the two identical models have diverged (after consolidating)"

    # save again
    sharded_optimizer2.consolidate_state_dict()
    state_dict2 = sharded_optimizer2.state_dict()

    # reload the state_dict
    sharded_optimizer2 = optim.OSS(model_oss2.parameters(), lr=0.1, momentum=0.99)
    sharded_optimizer2.load_state_dict(state_dict2)

    # take a step
    run_grad_step(device, model_oss1, sharded_optimizer1)
    run_grad_step(device, model_oss2, sharded_optimizer2)

    # check that reloading a saved state dict does not change the parameters
    for param1, param2 in zip(model_oss1.parameters(), model_oss2.parameters()):
        assert torch.allclose(param1, param2), "parameters of the two identical models have diverged (after reloading)"

    dist.destroy_process_group()


@skip_if_no_cuda
def test_state_dict_distributed():
    world_size = 8
    temp_file_name = tempfile.mkstemp()[1]

    if torch.cuda.is_available():
        world_size = min(world_size, torch.cuda.device_count())

    mp.spawn(
        run_state_dict_distributed, args=(world_size, temp_file_name), nprocs=world_size, join=True,
    )


def run_ddp_parity(rank, world_size, backend, temp_file_name):
    url = "file://" + temp_file_name
    dist.init_process_group(init_method=url, backend=backend, rank=rank, world_size=world_size)

    device = torch.device("cuda")
    torch.cuda.set_device(rank)
    torch.manual_seed(rank)
    np.random.seed(rank)

    def check_optimizer_equivalence(optimizer: Type[torch.optim.Optimizer]):
        # Any model works. Add one different buffer per rank
        model = torch.nn.Sequential(torch.nn.Linear(2, 3), torch.nn.Linear(3, 3), torch.nn.Linear(3, 3),)
        model.register_buffer("test_buffer", torch.ones((1)) * rank)
        model.to(device)

        sharded_optimizer = optim.OSS(params=model.parameters(), optim=optimizer, lr=1e-3)
        sharded_ddp_model = DDP(module=model, device_ids=[rank], broadcast_buffers=True)

        ddp_model_single = copy.deepcopy(model)
        ddp_optimizer = optimizer(ddp_model_single.parameters(), lr=1e-3)
        ddp_model = DDP(ddp_model_single, device_ids=[rank], broadcast_buffers=True)

        def check_same_model_params():
            for pg, ddp_pg in zip(sharded_optimizer.param_groups, ddp_optimizer.param_groups):
                for p, ddp_p in zip(pg["params"], ddp_pg["params"]):
                    assert torch.allclose(
                        p, ddp_p, atol=1e-3
                    ), f"Model parameters differ in between Pytorch optim and OSS \n{p} {ddp_p}\nworld size {world_size}"

            for b, ddp_b in zip(sharded_ddp_model.buffers(), ddp_model.buffers()):
                assert torch.allclose(
                    b, ddp_b
                ), f"Model buffers differ in between Pytorch optim and OSS\nworld size {world_size}"

        # The model should be synchronized in between the ranks at construction time, check that
        check_same_model_params()

        # The models should stay the same in between the ranks
        for i in range(20):
            input_tensor = torch.rand((64, 2)).to(device)

            def closure_ddp(input_tensor=input_tensor):
                ddp_optimizer.zero_grad()
                ddp_loss = ddp_model(input_tensor).abs().sum()
                ddp_loss.backward()
                return ddp_loss

            def closure_sharded(input_tensor=input_tensor):
                sharded_optimizer.zero_grad()
                sharded_loss = sharded_ddp_model(input_tensor).abs().sum()
                sharded_loss.backward()
                return sharded_loss

            loss_ddp = cast(torch.Tensor, ddp_optimizer.step(closure=closure_ddp))
            loss_sharded_optim = cast(torch.Tensor, sharded_optimizer.step(closure=closure_sharded))

            assert torch.allclose(
                loss_ddp, loss_sharded_optim
            ), f"Losses differ in between Pytorch optim and OSS\nworld size {world_size}"

            check_same_model_params()

    for opt in [torch.optim.SGD, torch.optim.Adam]:
        check_optimizer_equivalence(opt)

    dist.destroy_process_group()


@skip_if_no_cuda
@skip_if_single_gpu
def test_ddp_parity():
    temp_file_name = tempfile.mkstemp()[1]
    world_size = torch.cuda.device_count()
    backend = dist.Backend.NCCL
    mp.spawn(run_ddp_parity, args=(world_size, backend, temp_file_name), nprocs=world_size, join=True)
