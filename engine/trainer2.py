# encoding: utf-8
"""
@author:  sherlock
@contact: sherlockliao01@gmail.com
"""

import logging

import torch
import torch.nn as nn
from ignite.engine import Engine, Events
from ignite.handlers import ModelCheckpoint, Timer
from ignite.metrics import RunningAverage

from utils.reid_metric import R1_mAP

global ITER
ITER = 0

def create_supervised_trainer_with_center(
    model, 
    center_criterion, 
    cluster_criterion,#
    optimizer, 
    optimizer_center,
    optimizer_cluster,# 
    loss_fn, 
    loss_cluster_fn,#
    center_loss_weight,
    cluster_loss_weight,
    target_train_loader,#
    logger,
    device=None):
    """
    Factory function for creating a trainer for supervised models

    Args:
        model (`torch.nn.Module`): the model to train
        optimizer (`torch.optim.Optimizer`): the optimizer to use
        loss_fn (torch.nn loss function): the loss function to use
        device (str, optional): device type specification (default: None).
            Applies to both model and batches.

    Returns:
        Engine: a trainer engine with supervised update function
    """
    if device:
        if torch.cuda.device_count() > 1:
            model = nn.DataParallel(model)
        model.to(device)
    target_train_loader_iter = iter(target_train_loader)

    def _update(engine, batch):
        model.train()
        optimizer.zero_grad()
        optimizer_center.zero_grad()
        img, target = batch
        #源域图片
        img = img.to(device) if torch.cuda.device_count() >= 1 else img
        #源域标签
        target = target.to(device) if torch.cuda.device_count() >= 1 else target
        score, feat = model(img)
        loss = loss_fn(score, feat, target)
        global ITER
        if engine.state.epoch >= 30:
            #获取目标数据集batch
            try:
                target_img = next(target_train_loader_iter)[0]
            except:
                target_train_loader_iter = iter(target_train_loader)
                target_img = next(target_train_loader_iter)[0]
            #目标域图片
            target_img = target_img.to(device) if torch.cuda.device_count() >= 1 else target_img
            optimizer_cluster.zero_grad()
            _,target_feat = model(target_img,for_cluster = True)
            loss_cluster = loss_cluster_fn(target_feat)
            loss += loss_cluster
            if ITER == 0:
                logger.info("Total loss is {}, center loss is {}, cluster loss is {}".format(loss, center_loss_weight*center_criterion(feat, target),loss_cluster))
        else:
            if ITER == 0:
                logger.info("Total loss is {}, center loss is {}".format(loss, center_criterion(feat, target)))
        loss.backward()
        optimizer.step()
        for param in center_criterion.parameters():
            param.grad.data *= (1. / center_loss_weight)
        optimizer_center.step()
        if engine.state.epoch >= 30:
            for param in cluster_criterion.parameters():
                param.grad.data *= (1. / cluster_loss_weight)
            optimizer_cluster.step()

        # compute acc
        acc = (score.max(1)[1] == target).float().mean()
        return loss.item(), acc.item()

    return Engine(_update)


def create_supervised_evaluator(model, metrics,
                                device=None):
    """
    Factory function for creating an evaluator for supervised models

    Args:
        model (`torch.nn.Module`): the model to train
        metrics (dict of str - :class:`ignite.metrics.Metric`): a map of metric names to Metrics
        device (str, optional): device type specification (default: None).
            Applies to both model and batches.
    Returns:
        Engine: an evaluator engine with supervised inference function
    """
    if device:
        if torch.cuda.device_count() > 1:
            model = nn.DataParallel(model)
        model.to(device)

    def _inference(engine, batch):
        model.eval()
        with torch.no_grad():
            data, pids, camids = batch
            data = data.to(device) if torch.cuda.device_count() >= 1 else data
            feat = model(data)
            return feat, pids, camids

    engine = Engine(_inference)

    for name, metric in metrics.items():
        metric.attach(engine, name)

    return engine


def do_train_with_center2(
        cfg,
        model,
        center_criterion,
        cluster_criterion,  #
        train_loader,
        val_loader,
        target_train_loader,#
        target_val_loader,
        optimizer,
        optimizer_center,
        optimizer_cluster,  #
        scheduler,      
        loss_fn,
        loss_cluster_fn,  #
        num_query,
        start_epoch     
):
    log_period = cfg.SOLVER.LOG_PERIOD
    checkpoint_period = cfg.SOLVER.CHECKPOINT_PERIOD
    eval_period = cfg.SOLVER.EVAL_PERIOD
    output_dir = cfg.OUTPUT_DIR
    device = cfg.MODEL.DEVICE
    epochs = cfg.SOLVER.MAX_EPOCHS

    logger = logging.getLogger("reid_baseline.train")
    logger.info("Start training")
    trainer = create_supervised_trainer_with_center(
        model, 
        center_criterion, 
        cluster_criterion,#
        optimizer, 
        optimizer_center,
        optimizer_cluster,# 
        loss_fn, 
        loss_cluster_fn,#
        cfg.SOLVER.CENTER_LOSS_WEIGHT,
        cfg.SOLVER.CLUSTER_LOSS_WEIGHT,
        target_train_loader,# 
        logger,      
        device=device
        )
    evaluator = create_supervised_evaluator(model, metrics={'r1_mAP': R1_mAP(num_query, max_rank=50, feat_norm=cfg.TEST.FEAT_NORM)}, device=device)
    checkpointer = ModelCheckpoint(output_dir, cfg.MODEL.NAME, checkpoint_period, n_saved=10, require_empty=False)
    timer = Timer(average=True)

    trainer.add_event_handler(Events.EPOCH_COMPLETED, checkpointer, {'model': model,
                                                                     'optimizer': optimizer,
                                                                     'center_param': center_criterion,
                                                                     'optimizer_center': optimizer_center,
                                                                     'optimizer_cluster': optimizer_cluster,
                                                                     'cluster_param': cluster_criterion})

    timer.attach(trainer, start=Events.EPOCH_STARTED, resume=Events.ITERATION_STARTED,
                 pause=Events.ITERATION_COMPLETED, step=Events.ITERATION_COMPLETED)

    # average metric to attach on trainer
    RunningAverage(output_transform=lambda x: x[0]).attach(trainer, 'avg_loss')
    RunningAverage(output_transform=lambda x: x[1]).attach(trainer, 'avg_acc')

    @trainer.on(Events.STARTED)
    def start_training(engine):
        engine.state.epoch = start_epoch
        evaluator.run(val_loader)
        cmc, mAP = evaluator.state.metrics['r1_mAP']
        logger.info("Source Validation Results - Epoch: {}".format(engine.state.epoch))
        logger.info("mAP: {:.1%}".format(mAP))
        for r in [1, 5, 10]:
            logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))
        evaluator.run(target_val_loader)
        cmc, mAP = evaluator.state.metrics['r1_mAP']
        logger.info("Target Validation Results - Epoch: {}".format(engine.state.epoch))
        logger.info("mAP: {:.1%}".format(mAP))
        for r in [1, 5, 10]:
            logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))

    @trainer.on(Events.EPOCH_STARTED)
    def adjust_learning_rate(engine):
        scheduler.step()

    @trainer.on(Events.ITERATION_COMPLETED)
    def log_training_loss(engine):
        global ITER
        ITER += 1

        if ITER % log_period == 0:
            logger.info("Epoch[{}] Iteration[{}/{}] Loss: {:.3f}, Acc: {:.3f}, Base Lr: {:.2e}"
                        .format(engine.state.epoch, ITER, len(train_loader),
                                engine.state.metrics['avg_loss'], engine.state.metrics['avg_acc'],
                                scheduler.get_lr()[0]))
        if len(train_loader) == ITER:
            ITER = 0

    # adding handlers using `trainer.on` decorator API
    @trainer.on(Events.EPOCH_COMPLETED)
    def print_times(engine):
        logger.info('Epoch {} done. Time per batch: {:.3f}[s] Speed: {:.1f}[samples/s]'
                    .format(engine.state.epoch, timer.value() * timer.step_count,
                            train_loader.batch_size / timer.value()))
        logger.info('-' * 10)
        timer.reset()

    @trainer.on(Events.EPOCH_COMPLETED)
    def log_validation_results(engine):
        if engine.state.epoch % eval_period == 0:
            evaluator.run(val_loader)
            cmc, mAP = evaluator.state.metrics['r1_mAP']
            logger.info("Source Validation Results - Epoch: {}".format(engine.state.epoch))
            logger.info("mAP: {:.1%}".format(mAP))
            for r in [1, 5, 10]:
                logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))
            evaluator.run(target_val_loader)
            cmc, mAP = evaluator.state.metrics['r1_mAP']
            logger.info("Target Validation Results - Epoch: {}".format(engine.state.epoch))
            logger.info("mAP: {:.1%}".format(mAP))
            for r in [1, 5, 10]:
                logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))

    trainer.run(train_loader, max_epochs=epochs)
