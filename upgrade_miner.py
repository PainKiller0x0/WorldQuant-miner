import os
import re
import subprocess
import time
import sys

# --- 配置 ---
LLM_LOCAL_FILENAME = "llm_local.py"
GENERATOR_FILENAME = "alpha_generator_ollama.py"

# --- 1. 完美的 llm_local.py 代码 (包含 Mixin, 接口, 和温度修复) ---
PERFECT_LLM_LOCAL = r'''import torch
import os
import re
import logging
from transformers import AutoTokenizer, PretrainedConfig, PreTrainedModel
from transformers.generation.utils import GenerationMixin
import torch.nn as nn
import math

logger = logging.getLogger(__name__)

# --- MiniMind 模型定义 ---
class MiniMindConfig(PretrainedConfig):
    model_type = "minimind"
    def __init__(self, dim=512, n_layers=8, n_heads=8, vocab_size=6400, max_seq_len=512, **kwargs):
        super().__init__(**kwargs)
        self.dim = dim
        self.n_layers = n_layers
        self.n_heads = n_heads
        self.vocab_size = vocab_size
        self.max_seq_len = max_seq_len
        self.hidden_size = dim
        self.num_hidden_layers = n_layers
        self.num_attention_heads = n_heads

class RMSNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.eps = 1e-6
        self.weight = nn.Parameter(torch.ones(dim))
    def forward(self, x):
        norm_x = x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)
        return norm_x * self.weight

class FeedForward(nn.Module):
    def __init__(self, config):
        super().__init__()
        hidden_dim = 4 * config.dim
        self.w1 = nn.Linear(config.dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, config.dim, bias=False)
        self.w3 = nn.Linear(config.dim, hidden_dim, bias=False)
    def forward(self, x):
        return self.w2(torch.nn.functional.silu(self.w1(x)) * self.w3(x))

class Attention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.n_heads = config.n_heads
        self.head_dim = config.dim // config.n_heads
        self.wq = nn.Linear(config.dim, config.dim, bias=False)
        self.wk = nn.Linear(config.dim, config.dim, bias=False)
        self.wv = nn.Linear(config.dim, config.dim, bias=False)
        self.wo = nn.Linear(config.dim, config.dim, bias=False)
        self.register_buffer("mask", torch.tril(torch.ones(config.max_seq_len, config.max_seq_len)).view(1, 1, config.max_seq_len, config.max_seq_len))

    def forward(self, x):
        bs, seq_len, _ = x.shape
        q = self.wq(x).view(bs, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        k = self.wk(x).view(bs, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        v = self.wv(x).view(bs, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.head_dim)
        scores = scores.masked_fill(self.mask[:, :, :seq_len, :seq_len] == 0, float('-inf'))
        probs = torch.softmax(scores, dim=-1)
        output = torch.matmul(probs, v).transpose(1, 2).contiguous().view(bs, seq_len, -1)
        return self.wo(output)

class MiniMindBlock(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.attention = Attention(config)
        self.feed_forward = FeedForward(config)
        self.attention_norm = RMSNorm(config.dim)
        self.ffn_norm = RMSNorm(config.dim)
    def forward(self, x):
        x = x + self.attention(self.attention_norm(x))
        x = x + self.feed_forward(self.ffn_norm(x))
        return x

class MiniMindForCausalLM(PreTrainedModel, GenerationMixin):
    config_class = MiniMindConfig
    def __init__(self, config):
        super().__init__(config)
        self.tok_embeddings = nn.Embedding(config.vocab_size, config.dim)
        self.layers = nn.ModuleList([MiniMindBlock(config) for _ in range(config.n_layers)])
        self.norm = RMSNorm(config.dim)
        self.output = nn.Linear(config.dim, config.vocab_size, bias=False)
        self.output.weight = self.tok_embeddings.weight 

    def forward(self, input_ids, labels=None, **kwargs):
        h = self.tok_embeddings(input_ids)
        for layer in self.layers:
            h = layer(h)
        h = self.norm(h)
        logits = self.output(h)
        from transformers.modeling_outputs import CausalLMOutputWithPast
        return CausalLMOutputWithPast(loss=None, logits=logits)

    def prepare_inputs_for_generation(self, input_ids, **kwargs):
        return {"input_ids": input_ids}

    def get_input_embeddings(self):
        return self.tok_embeddings

    def set_input_embeddings(self, value):
        self.tok_embeddings = value

    def get_output_embeddings(self):
        return self.output

    def set_output_embeddings(self, new_embeddings):
        self.output = new_embeddings

class LocalLLM:
    def __init__(self, model_dir="./local_model"):
        self.model_dir = model_dir
        self.device = 'cpu' 
        self.model = None
        self.tokenizer = None
        self._load_model()

    def _load_model(self):
        try:
            logger.info(f"📂 [LocalLLM] 正在加载本地模型: {self.model_dir}")
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
            config = MiniMindConfig()
            if not hasattr(config, 'num_hidden_layers'):
                config.num_hidden_layers = config.n_layers
                config.hidden_size = config.dim
                config.num_attention_heads = config.n_heads
            
            self.model = MiniMindForCausalLM(config).to(self.device)
            weight_path = os.path.join(self.model_dir, "miner_zero.pth")
            
            if not os.path.exists(weight_path):
                logger.error(f"❌ 权重文件不存在: {weight_path}")
                return

            state_dict = torch.load(weight_path, map_location=self.device)
            self.model.load_state_dict(state_dict, strict=False)
            self.model.eval()
            logger.info(f"✅ [LocalLLM] Miner-Zero 加载成功！")
            
        except Exception as e:
            logger.critical(f"❌ [LocalLLM] 模型加载失败: {e}", exc_info=True)

    def generate(self, prompt, temp=1.0):
        if not self.model or not self.tokenizer: return None
        try:
            inputs = self.tokenizer(prompt, return_tensors='pt').to(self.device)
            with torch.no_grad():
                output_ids = self.model.generate(
                    inputs['input_ids'], 
                    max_new_tokens=200, 
                    temperature=temp,        
                    top_k=50,               
                    top_p=0.9, 
                    repetition_penalty=1.2, 
                    do_sample=True, 
                    pad_token_id=self.tokenizer.eos_token_id
                )
            full_text = self.tokenizer.decode(output_ids[0], skip_special_tokens=True)
            generated = full_text[len(prompt):].strip()
            
            if len(generated) < 3: 
                return None

            if ";" in generated:
                parts = generated.split(';')
                for p in parts:
                    if len(p) > 3: return p.strip() + ";"
                return parts[0].strip() + ";"
            return generated + ";"
        except Exception as e:
            logger.error(f"生成失败: {e}")
            return None
'''

# --- 2. 增强版 Generator 逻辑 ---
def patch_generator_file():
    if not os.path.exists(GENERATOR_FILENAME):
        print(f"❌ 找不到文件 {GENERATOR_FILENAME}")
        return False
    
    with open(GENERATOR_FILENAME, 'r', encoding='utf-8') as f:
        content = f.read()

    # 定义要替换的原始逻辑块（特征是固定的 prompt_templates）
    # 使用较短的片段来定位，以提高容错率
    target_block_start = 'if use_local and self.local_llm:'
    
    # 新的增强逻辑
    new_logic = '''        if use_local and self.local_llm:
            # [Fix] 动态提示词构建：随机注入算子，强制模型发散思维
            random_op = random.choice(['rank', 'ts_corr', 'ts_delta', 'ts_rank', 'decay_linear', 'signed_power'])
            random_field = random.choice(['close', 'open', 'volume', 'returns', 'vwap'])
            
            prompt_templates = [
                f"Generate a WorldQuant alpha expression using {random_op}.",
                f"Write a trading formula involving {random_field} and {random_op}.",
                f"Create a alpha factor that uses {random_op} operator.",
                f"Think of a new alpha expression based on {random_field}."
            ]
            prompt = random.choice(prompt_templates)
            
            # 使用高温度 (1.2) 激发创造力
            alpha_code = self.local_llm.generate(prompt, temp=1.2)
            if alpha_code:
                return {"expression": alpha_code, "settings": {}}
            else:
                logger.warning("[Miner] 本地模型生成失败，回退到在线 API。")
'''
    
    # 我们不直接替换文本块，因为缩进可能不同。我们采用更智能的替换方式
    # 查找原始代码中该 if 块的范围。
    # 简单起见，我们假设原始代码结构标准。如果找不到特定特征，就追加提示。
    
    # 为了保险，我们直接覆盖旧的 if use_local... 块
    # 原始代码通常长这样：
    #         if use_local and self.local_llm:
    #             prompt_templates = [ ... ]
    #             prompt = random.choice(prompt_templates)
    #             alpha_code = self.local_llm.generate(prompt, temp=1.2)
    #             if alpha_code: ...
    
    # 我们用正则匹配替换这段逻辑
    pattern = re.compile(r'if use_local and self\.local_llm:.*?return {"expression": alpha_code, "settings": {}}', re.DOTALL)
    
    if pattern.search(content):
        new_content = pattern.sub(new_logic.strip(), content) # strip去掉首尾空白，靠缩进对齐
        # 修正缩进：上面的 replacement 只有逻辑代码，我们需要确保它替换进去后缩进是对的
        # 由于正则匹配了整块，我们可以直接用 new_logic 替换，但要注意缩进对齐
        
        # 更稳妥的方法：全量替换那一段已知的“坏”代码
        # 你的原文件中那段代码大概是：
        bad_code_snippet = '''        if use_local and self.local_llm:
            prompt_templates = [
                "Generate a WorldQuant alpha expression.",
                "Write a valid alpha factor using standard operators.",
                "Create a financial trading signal expression."
            ]
            prompt = random.choice(prompt_templates)
            alpha_code = self.local_llm.generate(prompt, temp=1.2)
            if alpha_code:
                return {"expression": alpha_code, "settings": {}}
            else:
                logger.warning("[Miner] 本地模型生成失败，回退到在线 API。")'''
        
        if bad_code_snippet.replace(" ", "") in content.replace(" ", ""):
             # 如果能匹配上（忽略空格），尝试直接替换
             # 这里简单暴力一点，直接替换文件内容
             pass
    
    # 由于正则匹配比较危险，容易因为空格挂掉，我决定采用最稳妥的策略：
    # 直接在文件中查找 `if use_local and self.local_llm:` 并在其后插入新逻辑，
    # 但这也很难。
    
    # 鉴于你发给我的文件内容是确定的，我直接使用精确替换
    original_part = """        if use_local and self.local_llm:
            prompt_templates = [
                "Generate a WorldQuant alpha expression.",
                "Write a valid alpha factor using standard operators.",
                "Create a financial trading signal expression."
            ]
            prompt = random.choice(prompt_templates)
            alpha_code = self.local_llm.generate(prompt, temp=1.2)
            if alpha_code:
                return {"expression": alpha_code, "settings": {}}
            else:
                logger.warning("[Miner] 本地模型生成失败，回退到在线 API。")"""
                
    if original_part in content:
        print("✅ 找到旧的生成逻辑，正在增强...")
        new_content = content.replace(original_part, new_logic)
        with open(GENERATOR_FILENAME, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    else:
        # 尝试模糊匹配 (针对可能的空格差异)
        # 如果找不到，说明可能已经被修改过，我们跳过或警告
        print("⚠️ 未能精确匹配到旧的 Generator 逻辑，可能已经被修改过。")
        # 尝试另一种常见版本
        alt_part = """        if use_local and self.local_llm:
            prompt_templates = [
                "Generate a WorldQuant alpha expression.",
                "Write a valid alpha factor using standard operators.",
                "Create a financial trading signal expression."
            ]
            prompt = random.choice(prompt_templates)
            alpha_code = self.local_llm.generate(prompt, temp=1.2)
            if alpha_code:
                return {"expression": alpha_code, "settings": {}}"""
        
        if alt_part in content:
             print("✅ 找到旧逻辑（无else分支版），正在增强...")
             new_content = content.replace(alt_part, new_logic)
             with open(GENERATOR_FILENAME, 'w', encoding='utf-8') as f:
                f.write(new_content)
             return True

        print("❌ 无法自动增强 Generator，请确认文件内容是否标准。")
        return False

# --- 主程序 ---
def main():
    print("🚀 开始 WorldQuant Miner 一键修复与升级...")
    
    # 1. 覆盖 llm_local.py
    print(f"📝 正在覆写 {LLM_LOCAL_FILENAME} (包含 Mixin 和 Temperature 修复)...")
    with open(LLM_LOCAL_FILENAME, 'w', encoding='utf-8') as f:
        f.write(PERFECT_LLM_LOCAL)
    print("✅ llm_local.py 已更新。")
    
    # 2. 修改 Generator
    print(f"🔧 正在增强 {GENERATOR_FILENAME} (解决策略重复问题)...")
    patch_generator_file()
    
    # 3. 恢复数据
    print("📥 正在尝试从云端恢复历史数据 (解决 Dashboard 空白)...")
    try:
        # 检查宿主机是否有 db 文件，没有则创建空的以免 docker 报错
        if not os.path.exists("wq_miner.db"):
            print("⚠️ 未检测到 wq_miner.db，创建一个空文件...")
            open("wq_miner.db", 'a').close()

        print("⏳ 执行 Docker 恢复命令 (可能需要几十秒)...")
        result = subprocess.run(
            ["docker", "compose", "exec", "miner", "python", "recover_from_cloud_v3.py"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("✅ 数据恢复成功！Dashboard 现在应该有数据了。")
            print(result.stdout[-200:]) # 打印最后一点输出
        else:
            print("❌ 数据恢复失败。错误信息：")
            print(result.stderr)
    except FileNotFoundError:
        print("❌ 找不到 docker 命令，请确保已安装 Docker。")
    except Exception as e:
        print(f"❌ 执行恢复脚本时出错: {e}")

    # 4. 重启服务
    print("🔄 正在重启 Miner 服务以应用更改...")
    subprocess.run(["docker", "compose", "restart", "miner"])
    
    print("\n🎉 升级完成！请刷新 Dashboard 查看。")
    print("提示：如果 Miner 仍在刷 '策略重复'，请等待几分钟让新逻辑生效，或再次手动重启容器。")

if __name__ == "__main__":
    main()