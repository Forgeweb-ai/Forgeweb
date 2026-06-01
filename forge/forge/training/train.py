"""
forge/training/train.py
=======================
Fine-tune Phi-3.5 Mini on Forge training data using Unsloth + QLoRA.
Run this on Google Colab (free T4 GPU) or RunPod (~$0.30/hr).

Steps:
  1. pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"
  2. pip install --no-deps trl peft accelerate bitsandbytes
  3. python forge/training/train.py --data data/generated/ --output models/forge-phi3.5-lora

After training:
  - Merge LoRA adapter into base model
  - Export to GGUF Q4_K_M quantization (~2.2GB)
  - Drop the GGUF into ./models/ and set MODEL_BACKEND=local
"""

import json
import argparse
from pathlib import Path
from dataclasses import asdict
from forge.config import config


def load_dataset(data_dir: str) -> list[dict]:
    """Load all JSONL files from the data directory."""
    examples = []
    data_path = Path(data_dir)

    for jsonl_file in data_path.glob("**/*.jsonl"):
        with open(jsonl_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        examples.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    print(f"Loaded {len(examples)} training examples from {data_dir}")
    return examples


def format_for_phi(example: dict) -> str:
    """
    Format a training example into Phi-3.5's chat template.
    This is the exact format the model expects during inference too.
    """
    instruction = example["instruction"]
    context     = example.get("input", "")
    response    = example["output"]

    user_content = instruction
    if context:
        user_content += f"\n{context}"

    return (
        f"<|system|>\n{get_system_prompt()}<|end|>\n"
        f"<|user|>\n{user_content}<|end|>\n"
        f"<|assistant|>\n{response}<|end|>"
    )


def get_system_prompt() -> str:
    from forge.model.base import CODEBASE_SYSTEM_PROMPT
    return CODEBASE_SYSTEM_PROMPT


def train(data_dir: str, output_dir: str):
    """Main training loop — requires Unsloth to be installed."""
    cfg = config.training

    try:
        from unsloth import FastLanguageModel
        from trl import SFTTrainer
        from transformers import TrainingArguments
        from datasets import Dataset
    except ImportError:
        print("ERROR: Install training dependencies first:")
        print("  pip install 'unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git'")
        print("  pip install --no-deps trl peft accelerate bitsandbytes")
        return

    # ── 1. Load base model with Unsloth (4-bit quantized for efficiency) ──────
    print(f"\n🔥 Loading base model: {cfg.model_id}")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name     = cfg.model_id,
        max_seq_length = cfg.max_seq_length,
        dtype          = None,
        load_in_4bit   = True,
    )

    # ── 2. Attach LoRA adapter ────────────────────────────────────────────────
    print("   Attaching LoRA adapter...")
    model = FastLanguageModel.get_peft_model(
        model,
        r                   = cfg.lora_r,
        lora_alpha          = cfg.lora_alpha,
        lora_dropout        = cfg.lora_dropout,
        target_modules      = ["q_proj", "k_proj", "v_proj", "o_proj",
                               "gate_proj", "up_proj", "down_proj"],
        bias                = "none",
        use_gradient_checkpointing = "unsloth",
        random_state        = 42,
    )

    # ── 3. Prepare dataset ────────────────────────────────────────────────────
    print(f"\n📚 Loading training data from {data_dir}...")
    raw = load_dataset(data_dir)
    formatted = [{"text": format_for_phi(ex)} for ex in raw]
    dataset = Dataset.from_list(formatted)

    split = dataset.train_test_split(test_size=0.02, seed=42)
    train_dataset = split["train"]
    eval_dataset  = split["test"]
    print(f"   Train: {len(train_dataset)} | Eval: {len(eval_dataset)}")

    # ── 4. Training arguments ─────────────────────────────────────────────────
    training_args = TrainingArguments(
        output_dir              = output_dir,
        num_train_epochs        = cfg.epochs,
        per_device_train_batch_size = cfg.batch_size,
        gradient_accumulation_steps = cfg.grad_accum,
        learning_rate           = cfg.lr,
        warmup_ratio            = cfg.warmup_ratio,
        lr_scheduler_type       = cfg.scheduler,
        bf16                    = cfg.bf16,
        fp16                    = not cfg.bf16,
        logging_steps           = cfg.logging_steps,
        save_steps              = cfg.save_steps,
        eval_steps              = cfg.eval_steps,
        evaluation_strategy     = "steps",
        save_strategy           = "steps",
        load_best_model_at_end  = True,
        report_to               = "none",
    )

    # ── 5. Train ──────────────────────────────────────────────────────────────
    trainer = SFTTrainer(
        model           = model,
        tokenizer       = tokenizer,
        train_dataset   = train_dataset,
        eval_dataset    = eval_dataset,
        dataset_text_field = "text",
        max_seq_length  = cfg.max_seq_length,
        args            = training_args,
    )

    print("\n🚀 Starting training...")
    trainer.train()

    # ── 6. Save LoRA adapter ──────────────────────────────────────────────────
    print(f"\n💾 Saving LoRA adapter to {output_dir}...")
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)

    # ── 7. Export to GGUF ─────────────────────────────────────────────────────
    print("\n📦 Exporting to GGUF (Q4_K_M)...")
    gguf_path = Path(output_dir).parent / "forge-phi3.5-Q4_K_M.gguf"
    model.save_pretrained_gguf(
        str(gguf_path.parent / "forge-phi3.5"),
        tokenizer,
        quantization_method = "q4_k_m",
    )
    print(f"\n✅ GGUF saved to {gguf_path}")
    print("   Set LOCAL_MODEL_PATH in .env and MODEL_BACKEND=local to run on-device")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fine-tune Phi-3.5 Mini for Forge")
    parser.add_argument("--data",   default="data/generated/",           help="Path to training data directory")
    parser.add_argument("--output", default="models/forge-phi3.5-lora",  help="Output path for LoRA adapter")
    args = parser.parse_args()
    train(args.data, args.output)
