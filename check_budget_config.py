import json
import os
import sys

# 配置文件路径
CONFIG_FILE = "system_config.json"

def fix_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 错误: 找不到 {CONFIG_FILE}，请确保你在项目根目录下运行此脚本。")
        return

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.strip():
                print("⚠️ 配置文件为空，跳过修复。")
                return
            data = json.loads(content)
        
        modified = False
        
        # 1. 检查是否存在 llm_budgets (双轨制预算核心)
        if "llm_budgets" not in data:
            print("🔧 发现旧版配置，正在添加 'llm_budgets' 字段...")
            data["llm_budgets"] = {
                "miner": {
                    "daily_limit": 3000, 
                    "used_today": 0, 
                    "last_used_date_utc": "2024-01-01"
                },
                "evolver": {
                    "daily_limit": 1000, 
                    "used_today": 0, 
                    "last_used_date_utc": "2024-01-01"
                }
            }
            modified = True
        else:
            print("✅ 'llm_budgets' 字段已存在。")
            
            # 额外检查内部结构是否完整
            budgets = data["llm_budgets"]
            if "miner" not in budgets:
                budgets["miner"] = {"daily_limit": 3000, "used_today": 0, "last_used_date_utc": "2024-01-01"}
                modified = True
            if "evolver" not in budgets:
                budgets["evolver"] = {"daily_limit": 1000, "used_today": 0, "last_used_date_utc": "2024-01-01"}
                modified = True

        # 2. 清理可能引起混淆的旧字段 (可选，视情况而定，为了安全暂不删除旧字段，只确保新字段存在)
        
        if modified:
            # 写入文件
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            print(f"🎉 修复完成！已将更新后的配置写入 {CONFIG_FILE}。")
        else:
            print("👌 配置文件结构正确，无需修改。")

    except json.JSONDecodeError:
        print(f"❌ {CONFIG_FILE} JSON 格式错误，请检查文件内容是否损坏。")
    except Exception as e:
        print(f"❌ 发生未知错误: {e}")

if __name__ == "__main__":
    fix_config()