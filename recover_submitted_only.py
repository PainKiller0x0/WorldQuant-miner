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

def fetch_submitted_alphas(session):
    alphas = []
    offset = 0
    limit = 100
    base_url = "https://api.worldquantbrain.com/users/self/alphas"
    
    print("🚀 开始抓取 [已提交 (Submitted)] 策略...")
    
    while True:
        # 关键点：加上 status=SUBMITTED 过滤器
        params = {
            "limit": limit,
            "offset": offset,
            "order": "-dateCreated",
            "status": "ACTIVE"  # 只看已提交的
        }
        
        try:
            print(f"🔄 请求中: Offset {offset}...", end="\r")
            res = session.get(base_url, params=params)
            
            if res.status_code == 400:
                print(f"\n⚠️ 达到 API 分页上限 (10000条)。停止抓取。")
                break
            
            if res.status_code != 200:
                print(f"\n⚠️ API 返回错误: {res.status_code}")
                break
                
            data = res.json()
            results = data.get("results", [])
            
            if not results:
                print(f"\n✅ 已抓取所有已提交策略。")
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
            
    print(f"\n🎉 抓取结束，共找到 {len(alphas)} 条已提交策略。")
    return alphas

def save_submitted_to_db(alphas):
    if not os.path.exists(DB_FILE):
        print(f"❌ 数据库 {DB_FILE} 不存在！请先运行之前的恢复脚本初始化数据库。")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    added_count = 0
    updated_count = 0
    
    print("💾 正在同步到数据库...")
    
    for a in alphas:
        try:
            alpha_id = a.get("id")
            expr = None
            if "regular" in a and isinstance(a["regular"], dict):
                expr = a["regular"].get("code")
            if not expr: expr = a.get("code")
            
            if not expr: continue

            # 1. 先检查是否存在
            cursor.execute("SELECT id FROM alphas WHERE expression = ?", (expr,))
            exists = cursor.fetchone()
            
            # 提取 Performance
            stats = a.get("is", {})
            if not stats and "performance" in a: stats = a["performance"]
            fitness = float(stats.get("fitness", 0) or 0)
            sharpe = float(stats.get("sharpe", 0) or 0)
            returns = float(stats.get("returns", 0) or 0)
            
            checks = stats.get("checks", [])
            if checks:
                pass_cnt = sum(1 for c in checks if c.get("result") == "PASS")
                fail_cnt = sum(1 for c in checks if c.get("result") == "FAIL")
                checks_summary = f"{pass_cnt} PASS / {fail_cnt} FAIL"
            else:
                checks_summary = "UNKNOWN"

            # 标记为已提交
            is_submitted = 1
            is_failed = 0
            sub_time = a.get("dateSubmitted") or datetime.now().isoformat()

            if exists:
                # 更新现有记录的状态
                cursor.execute("""
                    UPDATE alphas 
                    SET is_submitted = ?, 
                        submitted_timestamp = ?,
                        is_failed_on_wq = ?
                    WHERE expression = ?
                """, (is_submitted, sub_time, is_failed, expr))
                updated_count += 1
            else:
                # 插入新记录
                cursor.execute('''
                    INSERT INTO alphas (
                        id, expression, fitness, sharpe, returns, turnover, 
                        margin, long_count, short_count, turnover_limit, 
                        drawdown, book_size, pnl, checks_summary, grade, 
                        status, is_submitted, is_failed_on_wq, submitted_timestamp, raw_data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    alpha_id, expr, fitness, sharpe, returns,
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
                    "COMPLETE",
                    is_submitted, is_failed, sub_time,
                    json.dumps(a)
                ))
                added_count += 1
                
        except Exception as e:
            print(f"⚠️ 处理出错: {e}")
            
    conn.commit()
    conn.close()
    
    print("========================================")
    print(f"✅ 同步完成！")
    print(f"   - 新增入库: {added_count} 条")
    print(f"   - 更新状态: {updated_count} 条 (标记为已提交)")
    print("========================================")

def main():
    uid = WQ_USER_ID
    key = WQ_API_KEY
    
    if not uid or not key:
        try:
            if os.path.exists(".env"):
                with open(".env", "r") as f:
                    for line in f:
                        if "WQ_USER_ID=" in line: uid = line.strip().split("=")[1].strip()
                        if "WQ_API_KEY=" in line: key = line.strip().split("=")[1].strip()
        except: pass
    
    if not uid or not key:
        print("❌ 未找到 API Key配置")
        return

    sess = get_wq_session(uid, key)
    if not sess: return
    
    alphas = fetch_submitted_alphas(sess)
    if alphas:
        save_submitted_to_db(alphas)
        print("\n💡 提示：现在运行 export_for_training.py，训练数据将包含这些高质量的已提交策略！")

if __name__ == "__main__":
    main()