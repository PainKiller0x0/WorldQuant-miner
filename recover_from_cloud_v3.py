import requests
import json
import os
import time
import sqlite3
from datetime import datetime

# ================= 配置区域 =================
WQ_USER_ID = os.getenv("WQ_USER_ID", "") 
WQ_API_KEY = os.getenv("WQ_API_KEY", "") 
# ===========================================

DB_FILE = "wq_miner.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS alphas (
        id TEXT PRIMARY KEY,
        expression TEXT NOT NULL,
        fitness REAL,
        sharpe REAL,
        returns REAL,
        turnover REAL,
        margin REAL,
        long_count INTEGER,
        short_count INTEGER,
        turnover_limit REAL,
        drawdown REAL,
        book_size REAL,
        pnl REAL,
        checks_summary TEXT,
        grade TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        raw_data JSON,
        UNIQUE(expression)
    )
    ''')
    conn.commit()
    return conn

def get_wq_session(user, key):
    s = requests.Session()
    s.auth = (user, key)
    print(f"正在尝试登录 WorldQuant (User: {user})...")
    try:
        res = s.post("https://api.worldquantbrain.com/authentication")
        if res.status_code in [200, 201]:
            print("✅ 登录成功！")
            return s
        else:
            print(f"❌ 登录失败: {res.status_code} {res.text}")
            return None
    except Exception as e:
        print(f"❌ 网络连接错误: {e}")
        return None

def fetch_all_alphas_v3(session):
    alphas = []
    offset = 0
    limit = 100
    # [v3] 回归正确的 API 端点
    base_url = "https://api.worldquantbrain.com/users/self/alphas"
    
    print("🚀 [v3] 开始无差别抓取 (Target: /users/self/alphas, No Filters)...")
    
    while True:
        # [v3] 移除 status 参数，只保留分页
        params = {
            "limit": limit,
            "offset": offset,
            "order": "-dateCreated" # 按时间倒序，先抓新的
        }
        
        try:
            print(f"🔄 请求中: Offset {offset}...", end="\r")
            res = session.get(base_url, params=params)
            
            if res.status_code != 200:
                print(f"\n⚠️ API 返回错误: {res.status_code}")
                print(f"   响应内容: {res.text[:200]}")
                break
                
            data = res.json()
            results = data.get("results", [])
            count = data.get("count", "Unknown")
            
            if offset == 0:
                print(f"\n📊 服务器显示总共有 {count} 条策略。")

            if not results:
                print(f"\n⚠️ 本页无数据，停止抓取。")
                break
                
            print(f"📥 本批次抓取 {len(results)} 条... (总计: {len(alphas) + len(results)})")
            
            for item in results:
                alphas.append(item)
            
            if len(results) < limit:
                break
                
            offset += limit
            time.sleep(0.5)
            
        except Exception as e:
            print(f"\n❌ 发生异常: {e}")
            break
            
    print(f"\n🎉 抓取结束，内存中现有 {len(alphas)} 条策略。")
    return alphas

def save_to_db(conn, alphas):
    cursor = conn.cursor()
    count = 0
    skipped_bad = 0
    
    print("💾 开始存入数据库...")
    
    for a in alphas:
        try:
            alpha_id = a.get("id")
            # 尝试提取代码
            expr = None
            if "regular" in a and isinstance(a["regular"], dict):
                expr = a["regular"].get("code")
            if not expr:
                expr = a.get("code") 
            
            if not expr:
                # 如果列表中没有代码详情，可能需要二次请求
                # 但通常 users/self/alphas 会包含 summary
                # 暂时跳过无代码的
                continue

            # 提取 Performance
            stats = a.get("is", {})
            if not stats and "performance" in a:
                stats = a["performance"]
            
            fitness = float(stats.get("fitness", 0) or 0)
            sharpe = float(stats.get("sharpe", 0) or 0)
            returns = float(stats.get("returns", 0) or 0)
            
            # [v3] 宽松过滤：只要不是严重错误的都收录
            # 即使是 Fail 的，只要有代码，Evolver 就能用
            if "error" in str(a.get("status", "")).lower():
                # 也可以存入，标记为 FAIL，供 Miner 避坑
                pass

            checks = stats.get("checks", [])
            pass_cnt = 0
            fail_cnt = 0
            if checks:
                pass_cnt = sum(1 for c in checks if c.get("result") == "PASS")
                fail_cnt = sum(1 for c in checks if c.get("result") == "FAIL")
                checks_summary = f"{pass_cnt} PASS / {fail_cnt} FAIL"
            else:
                checks_summary = "UNKNOWN"

            # 构造 status
            # 既然是在 Unsubmitted 列表，我们标记为 COMPLETE 方便 Evolver 读取
            # 或者如果 performance 都在，就可以视为 COMPLETE
            db_status = "COMPLETE"

            cursor.execute('''
                INSERT OR IGNORE INTO alphas (
                    id, expression, fitness, sharpe, returns, turnover, 
                    margin, long_count, short_count, turnover_limit, 
                    drawdown, book_size, pnl, checks_summary, grade, 
                    status, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                alpha_id,
                expr,
                fitness,
                sharpe,
                returns,
                float(stats.get("turnover", 0) or 0),
                float(stats.get("margin", 0) or 0),
                int(stats.get("longCount", 0) or 0),
                int(stats.get("shortCount", 0) or 0),
                0,
                float(stats.get("drawdown", 0) or 0),
                float(stats.get("bookSize", 0) or 0),
                float(stats.get("pnl", 0) or 0),
                checks_summary,
                a.get("grade", "UNKNOWN"),
                db_status,
                json.dumps(a)
            ))
            
            if cursor.rowcount > 0:
                count += 1
                
        except Exception as e:
            print(f"⚠️ 单条处理出错: {e}")
            
    conn.commit()
    print("========================================")
    print(f"✅ 最终结果: 成功恢复 {count} 条策略！")
    print("========================================")

def main():
    uid = WQ_USER_ID
    key = WQ_API_KEY
    
    # 尝试从 .env 读取
    if not uid or not key:
        try:
            if os.path.exists(".env"):
                with open(".env", "r") as f:
                    for line in f:
                        if "WQ_USER_ID=" in line: uid = line.strip().split("=")[1].strip()
                        if "WQ_API_KEY=" in line: key = line.strip().split("=")[1].strip()
        except: pass
    
    if not uid or not key:
        print("❌ 请先在 .env 文件中配置 WQ_USER_ID 和 WQ_API_KEY")
        return

    sess = get_wq_session(uid, key)
    if not sess: return
    
    conn = init_db()
    alphas = fetch_all_alphas_v3(sess)
    
    if alphas:
        save_to_db(conn, alphas)
        print("\n🚀 恢复完成！请执行以下命令重启服务：")
        print("docker-compose up -d --build --force-recreate")
    else:
        print("😭 依然没有抓到数据。这不科学！")
        print("请确认你的 .env 里的账号就是你网页登录的那个账号。")
    
    conn.close()

if __name__ == "__main__":
    main()