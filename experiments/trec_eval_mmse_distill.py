#! /usr/bin/env python3

"""
Usage: trec_eval_mmse_distill.py [hydra-conf-parameters...]

Evaluates the student model of a run of Margin-MSE distillation using the provided parameters.

Example: The following evalutes run called `[name]` on TREC DL '19 passage, which is located at `[path-to-dataset]` (if
it does not exist there, it is downloaded). For the ranking definition the hydra configuration [ranker] is used and
evaluation is performed on the checkpoint loaded from [checkpoint-name].
Evaluation is performed on a single GPU.
```bash
./trec_eval_mmse_distill.py \
    ranker=[ranker] \
	run_name="[name]" \
	checkpoint="[checkpoint-name]" \
	processor_cache=null \
	model_cache=null \
	trainer.accelerator=gpu \
	trainer.precision=32 \
	datamodule=trec19pass \
	datamodule.data_dir="[path-to-dataset]" \
	datamodule.num_workers=12 \
```
"""

from collections import defaultdict
from pathlib import Path

import hydra
from hydra.utils import instantiate as hydra_inst
from omegaconf import DictConfig
from pytorch_lightning import (
    LightningDataModule,
    LightningModule,
    Trainer,
    seed_everything,
)
from ranking_utils import write_trec_eval_file
from tqdm import tqdm

import common
from common.trec_eval import load_run_from_file, trec_evaluation
from proposal import ProposedDataProcessor
from proposal.mmsecolbert import MMSEColBERTRanker
from proposal.mmsedistillation import MMSEDistill


@hydra.main(config_path="hydra_conf", config_name="trec_eval", version_base=None)
def main(config: DictConfig):
    seed_everything(config.seed)
    common.set_cuda_devices_env(config.used_gpus)

    result_path = Path(config.result_path.format(datamodule=config.datamodule._target_.split('.')[-1]))
    print(f"Result path: {result_path}")

    if not result_path.exists():
        checkpoint_path = Path(config.checkpoint_path)

        trainer = hydra_inst(config.trainer)
        assert isinstance(trainer, Trainer)
        assert trainer.num_devices == 1

        print("Fetching cache paths", flush=True)
        keys = {"precision": trainer.precision}
        cache_root = Path(config.cache_root)
        model_cache = cache_root / config.model_cache.format(**keys) if config.model_cache else None
        processor_cache = cache_root / config.processor_cache.format(**keys) if config.processor_cache else None
        print(f"Model cache: {model_cache}")
        print(f"Processor cache: {processor_cache}")

        print("Instantiating model", flush=True)
        student = hydra_inst(config.ranker.model, cache_dir=model_cache)
        # student = ProposedRanker.load_from_checkpoint(checkpoint_path / config.checkpoint, topk=config.topk)
        
        sproc = hydra_inst(config.ranker.data_processor, cache_dir=processor_cache, append_mask=8)
        assert isinstance(student, LightningModule)
        assert isinstance(sproc, ProposedDataProcessor)
        print(f"Running new test using:\n{student.hparams}", flush=True)

        teacher = MMSEColBERTRanker(lr=0.00003, warmup_steps=1000, hparams={"freeze_bert": True})

        # datamodule = hydra_inst(config.datamodule, data_processor=data_processor)
        datamodule = hydra_inst(config.datamodule, data_processor=sproc)
        assert isinstance(datamodule, LightningDataModule)
        # model = MMSEDistill(student=student, teacher=teacher, datamodule=datamodule)
        model = MMSEDistill.load_from_checkpoint(checkpoint_path / config.checkpoint, student=student, teacher=teacher, datamodule=datamodule)
        assert isinstance(model, LightningModule)
        #assert isinstance(datamodule, TREC2019Passage)
        
        print("Evaluating model")
        #predictions = trainer.predict(
        #    model=model, dataloaders=datamodule, return_predictions=True, ckpt_path=checkpoint_path / config.checkpoint
        #)
        predictions = trainer.predict(model=student, dataloaders=datamodule, return_predictions=True)
        ids = [(qid, did) for _, qid, did in datamodule.predict_dataset.ids()]
        result = defaultdict(dict[str, float])
        for entry in tqdm(predictions):
            for idx, score in zip(entry["indices"], entry["scores"]):
                q_id, doc_id = ids[idx]
                result[q_id][doc_id] = float(score)
        write_trec_eval_file(result_path, result, "test")
        result = dict(result)
    else:
        print(f"Loading past evaluation run from file {result_path}")
        result = load_run_from_file(result_path)
        datamodule = hydra_inst(config.datamodule, data_processor=None)

    qrels = datamodule.qrels()
    rl = datamodule.relevance_level()
    print(trec_evaluation(qrels, result, ["recip_rank", "map", "ndcg_cut.10", "ndcg_cut.20"], relevance_level=rl))


if __name__ == "__main__":
    main()
