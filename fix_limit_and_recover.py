import json
import os
import time
import shutil

# 引用之前的恢复脚本 (确保它在同一目录下)
try:
    import recover_from_cloud_v3
except ImportError:
    print("❌ 找不到 recover_from_cloud_v3.py！请确认该文件在当前目录下。")
    exit(1)

CONFIG_FILE = "system_config.json"
UTILS_FILE = "utils.py"

def patch_config():
    print("🔧 正在扩容数据库上限...")
    
    # 1. 修改运行时的 system_config.json
    if os.path.exists(CONFIG_FILE) and os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                content = f.read().strip()
                data = json.loads(content) if content else {}
            
            # 暴力扩容到 5万
            data["pool_limit_unsubmitted"] = 50000
            data["hopeful_pool_max_size"] = 50000
            
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=4)
            print(f"✅ {CONFIG_FILE} 已更新: 上限设为 50,000")
        except Exception as e:
            print(f"⚠️ 修改配置文件失败: {e}")
    else:
        # 如果文件不存在，创建一个带大上限的默认配置
        data = {
            "pool_limit_unsubmitted": 50000,
            "hopeful_pool_max_size": 50000,
            "pool_limit_submitted": 2000,
            # 保持其他默认值以免报错
            "miner_concurrency": 1, "evolver_concurrency": 1,
            "wq_api_limiter": {"current_tpm_limit": 60}
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"✅ 已创建新的 {CONFIG_FILE} (上限 50,000)")

    # 2. 修改源码 utils.py 中的默认值 (防止以后重建配置时回退)
    if os.path.exists(UTILS_FILE):
        with open(UTILS_FILE, 'r') as f:
            code = f.read()
        
        # 替换默认值 300 -> 50000
        new_code = code.replace('"pool_limit_unsubmitted": 300,', '"pool_limit_unsubmitted": 50000,')
        new_code = new_code.replace('"hopeful_pool_max_size": 300,', '"hopeful_pool_max_size": 50000,')
        
        if new_code != code:
            with open(UTILS_FILE, 'w') as f:
                f.write(new_code)
            print(f"✅ {UTILS_FILE} 源码默认值已更新。")
        else:
            print(f"ℹ️ {UTILS_FILE} 似乎已经修改过，跳过。")

def main():
    # 1. 停止容器 (必须！防止这边写那边删)
    print("🛑 正在停止容器...")
    os.system("docker-compose down")
    
    # 2. 修改上限
    patch_config()
    
    # 3. 重新从云端抓取
    print("\n☁️ 开始执行二次召回...")
    # 重新初始化 DB (不清空表，依靠 INSERT OR IGNORE 去重)
    conn = recover_from_cloud_v3.init_db()
    
    # 获取 Session
    # 自动读取环境变量
    uid = os.getenv("WQ_USER_ID") or recover_from_cloud_v3.WQ_USER_ID
    key = os.getenv("WQ_API_KEY") or recover_from_cloud_v3.WQ_API_KEY
    
    if not uid:
        # 尝试从 .env 读取
        try:
            with open(".env") as f:
                for line in f:
                    if "WQ_USER_ID=" in line: uid = line.split("=")[1].strip()
                    if "WQ_API_KEY=" in line: key = line.split("=")[1].strip()
        except: pass

    sess = recover_from_cloud_v3.get_wq_session(uid, key)
    if sess:
        alphas = recover_from_cloud_v3.fetch_all_alphas_v3(sess)
        if alphas:
            recover_from_cloud_v3.save_to_db(conn, alphas)
    
    conn.close()
    
    print("\n✅ 数据恢复完成！现在的数据库应该有 9902 条了。")
    print("💡 提示：现在运行 export_for_training.py 应该能导出全部数据了。")

if __name__ == "__main__":
    main()