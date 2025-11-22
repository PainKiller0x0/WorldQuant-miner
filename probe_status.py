import requests
import json
import os

# ================= 配置区域 =================
WQ_USER_ID = os.getenv("WQ_USER_ID", "") 
WQ_API_KEY = os.getenv("WQ_API_KEY", "") 
# ===========================================

def get_wq_session(user, key):
    s = requests.Session()
    s.auth = (user, key)
    try:
        res = s.post("https://api.worldquantbrain.com/authentication")
        if res.status_code in [200, 201]:
            return s
    except: pass
    return None

def probe():
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

    sess = get_wq_session(uid, key)
    if not sess:
        print("❌ 登录失败")
        return

    base_url = "https://api.worldquantbrain.com/users/self/alphas"
    
    # 可能的状态关键词列表
    candidates = [
        "SUBMITTED", "Submitted", "submitted",
        "PENDING", "Pending", "pending",
        "ACCEPTED", "Accepted", "accepted",
        "COMPLETED", "Completed", "completed",
        "QUEUED", "Queued",
        "active", "ACTIVE","actived", "ACTIVED",
        "SCORING", "Scoring"
    ]
    
    print("🔍 开始探测 API 状态参数...")
    
    found_any = False
    for status in candidates:
        params = {
            "limit": 1,
            "offset": 0,
            "status": status
        }
        try:
            res = sess.get(base_url, params=params)
            if res.status_code == 200:
                data = res.json()
                count = data.get("count", 0)
                results = len(data.get("results", []))
                
                if count > 0 or results > 0:
                    print(f"✅ 命中! status='{status}' -> 找到 {count} 条 (本页 {results} 条)")
                    found_any = True
                else:
                    print(f"⬜ status='{status}' -> 0 条")
            else:
                print(f"⚠️ status='{status}' -> API 错误 {res.status_code}")
        except Exception as e:
            print(f"❌ 异常: {e}")

    if not found_any:
        print("\n😭 所有常用状态都试过了，都没找到。")
        print("可能的原因：")
        print("1. 你的已提交策略非常古老，被归档了？")
        print("2. 它们的状态可能不是 'Submitted'，而是直接变成了 'Accepted' 或 'Rejected'？")
    else:
        print("\n💡 请记下上面命中的 status 关键词，修改 recover_submitted_only.py 中的 status 参数再试。")

if __name__ == "__main__":
    probe()