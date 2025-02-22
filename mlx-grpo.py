import mlx.core as mx
import mlx.nn as nn
from mlx.utils import tree_map
from datasets import load_dataset, Dataset
from transformers import AutoTokenizer  # Not using AutoModelForCausalLM anymore
from peft import LoraConfig
from trl import GRPOConfig, GRPOTrainer
from mlx_lm import load as mlx_load, generate as mlx_generate
import numpy as np
import os
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import math
from mlx.optimizers import Adam  # Use the new optimizer module
import re
import inspect  # ensure inspect is imported

# -------------------------------------------------------------------
# Dataset Preparation and Formatting
# -------------------------------------------------------------------
SYSTEM_PROMPT = """
Respond in the following format:

<reasoning>
...
</reasoning>
<answer>
...
</answer>
"""

XML_COT_FORMAT = """\
<reasoning>
{reasoning}
</reasoning>
<answer>
{answer}
</answer>
"""

def extract_xml_answer(text: str) -> str:
    answer = text.split("<answer>")[-1]
    answer = answer.split("</answer>")[0]
    return answer.strip()

def extract_hash_answer(text: str) -> str | None:
    if "####" not in text:
        return None
    return text.split("####")[1].strip()

# Uncomment the middle messages below for 1-shot prompting if desired.
def get_gsm8k_questions(split="train") -> Dataset:
    data = load_dataset('openai/gsm8k', 'main')[split]  # type: ignore
    data = data.map(lambda x: {
        'prompt': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            # {'role': 'user', 'content': 'What is the largest single-digit prime number?'},
            # {'role': 'assistant', 'content': XML_COT_FORMAT.format(
            #     reasoning="9 is divisible by 3 and 8 is divisible by 2, but 7 is prime.",
            #     answer="7"
            # )},
            {'role': 'user', 'content': x['question']}
        ],
        'answer': extract_hash_answer(x['answer'])
    })  # type: ignore
    return data  # type: ignore

dataset = get_gsm8k_questions()

# -------------------------------------------------------------------
# Reward Functions
# -------------------------------------------------------------------
def correctness_reward_func(prompts, completions, answer, **kwargs) -> list[float]:
    responses = [completion[0]['content'] for completion in completions]
    q = prompts[0][-1]['content']
    extracted_responses = [extract_xml_answer(r) for r in responses]
    print('-' * 20, f"Question:\n{q}", f"\nAnswer:\n{answer[0]}", f"\nResponse:\n{responses[0]}", f"\nExtracted:\n{extracted_responses[0]}")
    return [2.0 if r == a else 0.0 for r, a in zip(extracted_responses, answer)]

def int_reward_func(completions, **kwargs) -> list[float]:
    responses = [completion[0]['content'] for completion in completions]
    extracted_responses = [extract_xml_answer(r) for r in responses]
    return [0.5 if r.isdigit() else 0.0 for r in extracted_responses]

def strict_format_reward_func(completions, **kwargs) -> list[float]:
    pattern = r"^<reasoning>\n.*?\n</reasoning>\n<answer>\n.*?\n</answer>\n$"
    responses = [completion[0]["content"] for completion in completions]
    matches = [re.match(pattern, r) for r in responses]
    return [0.5 if match else 0.0 for match in matches]

def soft_format_reward_func(completions, **kwargs) -> list[float]:
    pattern = r"<reasoning>.*?</reasoning>\s*<answer>.*?</answer>"
    responses = [completion[0]["content"] for completion in completions]
    matches = [re.match(pattern, r) for r in responses]
    return [0.5 if match else 0.0 for match in matches]

def count_xml(text) -> float:
    count = 0.0
    if text.count("<reasoning>\n") == 1:
        count += 0.125
    if text.count("\n</reasoning>\n") == 1:
        count += 0.125
    if text.count("\n<answer>\n") == 1:
        count += 0.125
        count -= len(text.split("\n</answer>\n")[-1]) * 0.001
    if text.count("\n</answer>") == 1:
        count += 0.125
        count -= (len(text.split("\n</answer>")[-1]) - 1) * 0.001
    return count

def xmlcount_reward_func(completions, **kwargs) -> list[float]:
    contents = [completion[0]["content"] for completion in completions]
    return [count_xml(c) for c in contents]

# -------------------------------------------------------------------
# Model Configuration and Loading (Pure MLX)
# -------------------------------------------------------------------
# Use a model name (here using Qwen as an example)
model_name = "Qwen/Qwen2.5-1.5B-Instruct"
output_dir = "outputs/Qwen-1.5B-MLX-GRPO"
run_name = "Qwen-1.5B-MLX-GRPO-gsm8k"

# Update training args to be MLX compatible
training_args = {
    'output_dir': output_dir,
    'run_name': run_name,
    'learning_rate': 5e-6,
    'batch_size': 1,
    'gradient_accumulation_steps': 4,
    'num_epochs': 1,
    'warmup_ratio': 0.1,
    'max_grad_norm': 0.1,
    'logging_steps': 1
}

peft_config = LoraConfig(
    r=16,
    lora_alpha=64,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"],
    task_type="CAUSAL_LM",
    lora_dropout=0.05,
)

# Replace the model loading section
def load_model(model_name):
    """Load model and tokenizer"""
    model, tokenizer = mlx_load(model_name)
    tokenizer.pad_token = tokenizer.eos_token

    # Fix: Add a forward function to the model dictionary so that it becomes callable.
    def forward_fn(**tokens_dict):
        # Call the apply_fn with the learned parameters from the model dictionary.
        return model["apply_fn"](model["params"], **tokens_dict)
    model["forward"] = forward_fn

    return model, tokenizer

# Update the model loading call
model, tokenizer = load_model(model_name)

# -------------------------------------------------------------------
# Initialize and Run GRPO Training (Pure MLX)
# -------------------------------------------------------------------
@dataclass
class MLXGRPOConfig:
    """Configuration class for MLX GRPO training"""
    output_dir: str
    run_name: str
    learning_rate: float = 5e-6
    batch_size: int = 1
    gradient_accumulation_steps: int = 4
    num_epochs: int = 1
    warmup_ratio: float = 0.1
    max_grad_norm: float = 0.1
    logging_steps: int = 1
    num_generations: int = 16
    max_prompt_length: int = 256
    max_completion_length: int = 786
    adam_beta1: float = 0.9
    adam_beta2: float = 0.99
    weight_decay: float = 0.1
    lr_scheduler_type: str = 'cosine'
    save_steps: int = 100

class MLXGRPOTrainer:
    def __init__(self, model, tokenizer, reward_funcs, args: MLXGRPOConfig, train_dataset):
        self.model = model
        self.tokenizer = tokenizer
        self.reward_funcs = reward_funcs
        self.args = args
        self.train_dataset = train_dataset
        
        # Enhanced optimizer configuration
        self.optimizer = Adam(args.learning_rate, model.parameters())
        
        self.step = 0
        self.total_steps = len(train_dataset) * args.num_epochs
        
    def _get_scheduler(self):
        """Implements cosine learning rate schedule"""
        warmup_steps = int(self.total_steps * self.args.warmup_ratio)
        
        def lr_schedule(step):
            # Linear warmup
            if step < warmup_steps:
                return float(step) / float(max(1, warmup_steps))
            # Cosine decay
            progress = float(step - warmup_steps) / float(max(1, self.total_steps - warmup_steps))
            return 0.5 * (1.0 + math.cos(math.pi * progress))
            
        return self.args.learning_rate * lr_schedule
    
    def generate_responses(self, batch):
        """Generate responses for a batch of prompts"""
        responses = []
        
        def generate_fn():
            # Format the prompt from the messages
            messages = batch['prompt']
            formatted_prompt = ""
            for msg in messages:
                if msg['role'] == 'system':
                    formatted_prompt += f"System: {msg['content']}\n"
                elif msg['role'] == 'user':
                    formatted_prompt += f"User: {msg['content']}\n"
                elif msg['role'] == 'assistant':
                    formatted_prompt += f"Assistant: {msg['content']}\n"
            
            output = mlx_generate(
                self.model,
                self.tokenizer,
                formatted_prompt
            )
            # If output is already a dict with key "content", wrap it in a nested list.
            if isinstance(output, dict) and "content" in output:
                return [[ output ]]
            else:
                return [[ {"content": output} ]]
            
        for _ in range(self.args.num_generations):
            response = generate_fn()
            responses.append(response)
        
        return responses
    
    def compute_rewards(self, batch, responses):
        """Compute rewards using all reward functions"""
        all_rewards = []
        
        for reward_fn in self.reward_funcs:
            rewards = reward_fn(
                prompts=batch['prompt'],
                completions=responses,
                answer=batch.get('answer')
            )
            all_rewards.append(mx.array(rewards))
            
        # Combine rewards - mean across all reward functions
        combined_rewards = mx.mean(mx.stack(all_rewards), axis=0)
        
        # Normalize rewards
        mean_reward = mx.mean(combined_rewards)
        std_reward = mx.std(combined_rewards)
        normalized_rewards = (combined_rewards - mean_reward) / (std_reward + 1e-8)
        
        return normalized_rewards
    
    def save_checkpoint(self, path: str):
        """Save model checkpoint"""
        os.makedirs(path, exist_ok=True)
        
        # Save model weights
        mx.save(os.path.join(path, "model.safetensors"), self.model)
        
        # Save optimizer state
        mx.save(os.path.join(path, "optimizer.safetensors"), self.optimizer.state)
        
        # Save training state
        training_state = {
            "step": self.step,
            "args": self.args.__dict__
        }
        mx.save(os.path.join(path, "trainer_state.safetensors"), training_state)

    def train_step(self, batch):
        """Performs a single training step using GRPO."""
        # Generate multiple responses for each prompt
        responses = self.generate_responses(batch)
        
        # Compute rewards for each prompt.
        # Retrieve the reference answer from the batch (if available)
        reference = batch.get("answer", None)
        rewards = []
        for completions in responses:
            reward = 0
            for f in self.reward_funcs:
                sig = inspect.signature(f)
                call_args = {}
                # Build a dictionary based on expected parameter names.
                for param in sig.parameters:
                    if param in ("completions", "response", "prompts"):
                        call_args[param] = completions
                    elif param == "answer":
                        call_args[param] = reference
                    elif param == "batch":
                        call_args[param] = batch
                if not call_args:
                    # Fallback: assume the function accepts a single positional argument.
                    result = f(completions)
                else:
                    result = f(**call_args)
                if isinstance(result, list):
                    result = sum(result)
                reward += result
            rewards.append(reward)
        
        # Convert rewards to mx array
        rewards = mx.array(rewards)
        
        # Normalize rewards
        rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-8)
        
        # Compute loss and update model
        def loss_fn(model):
            # Tokenize the formatted prompt from the batch.
            messages = batch.get('prompt', [])
            formatted_prompt = ""
            for msg in messages:
                if msg['role'] == 'system':
                    formatted_prompt += f"System: {msg['content']}\n"
                elif msg['role'] == 'user':
                    formatted_prompt += f"User: {msg['content']}\n"
                elif msg['role'] == 'assistant':
                    formatted_prompt += f"Assistant: {msg['content']}\n"
            tokens_dict = self.tokenizer.encode_plus(
                    formatted_prompt,
                    return_tensors="np",
                    padding=True
                )
            # Call model using keyword arguments. Check different possibilities for invoking the model.
            if callable(model):
                output = model(**tokens_dict)
            elif isinstance(model, dict) and "forward" in model and callable(model["forward"]):
                output = model["forward"](**tokens_dict)
            elif hasattr(model, "forward") and callable(model.forward):
                output = model.forward(**tokens_dict)
            elif hasattr(model, "apply") and callable(model.apply):
                output = model.apply(**tokens_dict)
            else:
                raise ValueError("Model is not callable and has no forward or apply method.")

            # If output is a tuple/list, assume that the logits are the first element.
            if isinstance(output, (list, tuple)):
                logits = output[0]
            else:
                logits = output

            loss = -mx.mean(rewards * logits)
            return loss
            
        loss, grads = mx.value_and_grad(loss_fn)(self.model)
        
        # Apply gradients with gradient clipping
        grads = mx.tree_map(lambda g: mx.clip(g, -self.args.max_grad_norm, self.args.max_grad_norm), grads)
        self.optimizer.update(self.model, grads)
        self.step += 1
        
        return loss

    def train(self):
        """Enhanced training loop with proper logging and checkpointing"""
        print(f"Starting training with {self.total_steps} total steps")
        
        for epoch in range(self.args.num_epochs):
            for batch in self.train_dataset:
                self.step += 1
                
                # Training step
                loss = self.train_step(batch)
                
                # Logging
                if self.step % self.args.logging_steps == 0:
                    print(f"Epoch {epoch}, Step {self.step}/{self.total_steps}, Loss: {loss.item():.4f}")
                
                # Save checkpoint
                if self.step % self.args.save_steps == 0:
                    checkpoint_path = os.path.join(self.args.output_dir, f"checkpoint-{self.step}")
                    self.save_checkpoint(checkpoint_path)
                    print(f"Saved checkpoint to {checkpoint_path}")

def main():
    # Initialize configuration with all parameters
    config = MLXGRPOConfig(
        output_dir="output",
        run_name="mlx-grpo-run",
        num_generations=16,
        max_prompt_length=256,
        max_completion_length=786
    )
    
    # Load model and tokenizer
    model, tokenizer = load_model(model_name)
    
    # Initialize trainer
    trainer = MLXGRPOTrainer(
        model=model,
        tokenizer=tokenizer,
        reward_funcs=[
            xmlcount_reward_func,
            soft_format_reward_func,
            strict_format_reward_func,
            int_reward_func,
            correctness_reward_func
        ],
        args=config,
        train_dataset=dataset
    )
    
    # Start training
    trainer.train()

if __name__ == "__main__":
    main()
