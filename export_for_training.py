import sqlite3
import json
import random
import os

# 数据库路径
DB_FILE = "wq_miner.db"
# 输出的训练集文件
OUTPUT_FILE = "dataset_wq_alpha.jsonl"

def export_data():
    if not os.path.exists(DB_FILE):
        print("❌ 找不到数据库文件！")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 只提取表达式。既然是训练“生成能力”，我们暂时不管 fitness 高低，
    # 只要是合法的 WQ 代码，都是好的语言素材。
    # (进阶：可以只训练高分策略，让模型学习“好策略”的特征)
    cursor.execute("SELECT expression, fitness FROM alphas WHERE expression IS NOT NULL")
    rows = cursor.fetchall()
    
    print(f"🔍 扫描到 {len(rows)} 条数据...")
    
    dataset = []
    
    # 定义一些 Prompt 模板，增加数据的多样性
    prompts = [
        "Generate a WorldQuant alpha expression.",
        "Write a valid alpha factor using standard operators.",
        "Create a financial trading signal expression.",
        "Output a WorldQuant Brain alpha formula.",
        "Construct a new alpha factor."
    ]
    
    count = 0
    for row in rows:
        expr = row[0].strip()
        fitness = row[1]
        
        # 简单的过滤：太短的可能没意义
        if len(expr) < 10:
            continue
            
        # 确保以分号结尾
        if not expr.endswith(';'):
            expr += ';'
            
        # 构造 MiniMind/Alpaca 格式的数据
        # instruction: 指令
        # input: 输入 (通常为空)
        # output: 期望的模型输出 (即 Alpha 代码)
        
        data_point = {
            "instruction": random.choice(prompts),
            "input": "",
            "output": expr
        }
        
        dataset.append(data_point)
        count += 1

    # 写入 JSONL 文件
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for entry in dataset:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            
    print(f"✅ 成功导出 {count} 条训练数据到 {OUTPUT_FILE}")
    print("样本预览:")
    print(json.dumps(dataset[0], indent=2))

if __name__ == "__main__":
    export_data()