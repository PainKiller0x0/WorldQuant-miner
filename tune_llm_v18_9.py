import os
import re

# 把温度降到 0.6，Top-K 降到 20，让它更专注
CONTENT_GENERATE_METHOD = r'''    def generate(self, prompt, temp=1.0):
        if not self.model or not self.tokenizer: return None
        try:
            inputs = self.tokenizer(prompt, return_tensors='pt').to(self.device)
            with torch.no_grad():
                # [v18.9 Tuning] 极度保守模式
                output_ids = self.model.generate(
                    inputs['input_ids'], 
                    max_new_tokens=200, 
                    temperature=0.6,        # [Fix] 降温至 0.6，减少幻觉
                    top_k=20,               # [Fix] 只选概率最高的 20 个词
                    top_p=0.9, 
                    repetition_penalty=1.2, 
                    do_sample=True, 
                    pad_token_id=self.tokenizer.eos_token_id
                )
            full_text = self.tokenizer.decode(output_ids[0], skip_special_tokens=True)
            generated = full_text[len(prompt):].strip()
            
            # [v18.9] 放宽过滤：只要有长度就行
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

filename = "llm_local.py"
if os.path.exists(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    pattern = r"def generate\(self, prompt, temp=1.0\):[\s\S]*"
    new_content = re.sub(pattern, CONTENT_GENERATE_METHOD, content)
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(new_content)
        
    print("🚀 llm_local.py 已优化 (v18.9 保守模式)")
    print("正在重启 Miner...")
    os.system("docker-compose restart miner")
else:
    print("❌ 找不到 llm_local.py")