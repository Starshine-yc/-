# -*- coding: utf-8 -*-
"""
一、环境变量配置说明：
   kele_auth = 抓包提取的 udtauth40 凭证（必填）
   示例：0ee0gN9xMVsnJGOPMs1di9F...
   多账号（支持换行、@、& 分隔，支持加备注）：
     kele_auth = 大号#token1@小号#token2
   kele_proxy_api = 可选：品赞等动态代理提取 API 地址；默认直连试跑，首次结算金币成功后，才拉取代理并原地切换（一账号一IP，结算不成功不消耗代理IP）
   kele_proxy_static = 可选：静态代理地址（支持 host:port 或 host:port:user:pass）

二、入口地址（微信打开）：
   https://klrk1107134630-2.eos-shanghai-1.cmecloud.cn/index.html?m2z=o4n&rc4=onq&upuid=7979453
"""

# =======================【用户自定义配置区域】=======================
# 1. 模拟阅读相关配置
READ_NUM = 15             # 每轮阅读篇数（建议设在 10 ~ 20 之间）
MIN_WAIT_SECONDS = 9.0    # 单篇最少等待时间（秒）
MAX_WAIT_SECONDS = 12.5   # 单篇最多等待时间（秒）

# 2. 多账号运行模式与并发控制
ENABLE_CONCURRENT = False # 并发总开关：True 开启并发，False 按顺序依次执行
MAX_WORKERS = 3           # 最大并发线程数（建议 2~5，避免瞬时并发过高）

# 3. 自动提现相关配置
AUTO_WITHDRAW = True      # 自动提现总开关：True 开启，False 关闭
WITHDRAW_THRESHOLD = 0.3  # 满多少元自动提现（最低 0.3 元起提，即 3000 金币）
WITHDRAW_TYPE = "wx"      # 提现方式："wx" 提现到微信零钱，"ali" 提现到支付宝
# ===================================================================

import os
import re
import sys
import json
import time
import random
import threading
import urllib.parse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows 编码兼容
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

# 线程安全加锁打印
_original_print = print
_account_tag = threading.local()
_print_lock = threading.Lock()

def set_account_tag(tag):
    _account_tag.tag = tag

def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    tag = getattr(_account_tag, 'tag', '')
    if tag:
        args = (f"[{tag}]",) + args
    with _print_lock:
        _original_print(*args, **kwargs)

def notify_safe(title, content):
    """安全推送通知"""
    try:
        from notify import send
        send(title, content)
    except Exception:
        try:
            from ql_sendNotify import send
            send(title, content)
        except Exception as e:
            print(f"\n[通知跳过] 推送未配置或发送失败: {e}")

# ==================== 代理核心算法 ====================
PROXY_FETCH_RETRIES = 3
_PROXY_JSON_LIST_KEYS = ("data", "result", "list", "proxy_list", "rows", "items", "obj")
_PROXY_JSON_STR_KEYS = ("proxy", "server", "address")
_PROXY_HOST_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9\-\.]{0,253}[A-Za-z0-9])?$')

def _first_str(mapping, keys):
    for k in keys:
        v = mapping.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""

def _valid_host(host):
    return bool(host) and bool(_PROXY_HOST_RE.match(host))

def _valid_port(port):
    if not str(port).isdigit():
        return False
    return 1 <= int(port) <= 65535

def _make_endpoint(host, port, user=None, pwd=None):
    host = (host or "").strip()
    if not _valid_host(host) or not _valid_port(port):
        return None
    ep = {"host": host, "port": str(port)}
    if user:
        ep["user"] = str(user).strip()
        ep["pass"] = str(pwd or "").strip()
    return ep

def _parse_proxy_endpoint(raw):
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        if parsed.hostname and parsed.port:
            return _make_endpoint(parsed.hostname, parsed.port,
                                  urllib.parse.unquote(parsed.username) if parsed.username else None,
                                  urllib.parse.unquote(parsed.password or ""))
        return None
    parts = raw.split(":")
    if len(parts) == 2:
        return _make_endpoint(parts[0], parts[1])
    if len(parts) >= 4:
        return _make_endpoint(parts[0], parts[1], parts[2], ":".join(parts[3:]))
    parts = raw.split("|")
    if len(parts) == 3:
        hp = parts[0].split(":")
        if len(hp) >= 2:
            return _make_endpoint(hp[0], hp[-1], parts[1], parts[2])
    return None

def _proxy_from_json(val, depth=0):
    if depth > 6:
        return None
    if isinstance(val, str):
        return _parse_proxy_endpoint(val)
    if isinstance(val, list):
        for item in val:
            ep = _proxy_from_json(item, depth + 1)
            if ep:
                return ep
        return None
    if not isinstance(val, dict):
        return None
    host = _first_str(val, ("ip", "host", "server_ip", "proxy_ip"))
    port = _first_str(val, ("port", "proxy_port"))
    if host and port:
        user = _first_str(val, ("account", "user", "username", "proxy_user", "http_user"))
        pwd = _first_str(val, ("password", "pass", "pwd", "proxy_pass", "http_pass"))
        ep = _make_endpoint(host, port, user, pwd)
        if ep:
            return ep
    for key in _PROXY_JSON_STR_KEYS:
        if isinstance(val.get(key), str) and val[key].strip():
            ep = _parse_proxy_endpoint(val[key])
            if ep:
                if not ep.get("user"):
                    user = _first_str(val, ("account", "user", "username", "proxy_user", "http_user"))
                    pwd = _first_str(val, ("password", "pass", "pwd", "proxy_pass", "http_pass"))
                    if user:
                        ep["user"], ep["pass"] = user, pwd
                return ep
    for key in _PROXY_JSON_LIST_KEYS:
        if key in val:
            ep = _proxy_from_json(val[key], depth + 1)
            if ep:
                return ep
    for child in val.values():
        ep = _proxy_from_json(child, depth + 1)
        if ep:
            return ep
    return None

def parse_proxy_response(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        ep = _proxy_from_json(json.loads(text))
        if ep:
            return ep
    except (ValueError, TypeError):
        pass
    ipv4 = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    endpoints = []
    for line in re.split(r'[\r\n,;]+', text):
        ep = _parse_proxy_endpoint(line)
        if ep and (ipv4.match(ep["host"]) or "." in ep["host"]):
            endpoints.append(ep)
    if not endpoints:
        return None
    for ep in endpoints:
        if ipv4.match(ep["host"]):
            return ep
    return endpoints[0]

def _proxy_dict(ep):
    scheme = "http"
    if ep.get("user"):
        auth = f"{urllib.parse.quote(ep['user'], safe='')}:{urllib.parse.quote(ep['pass'], safe='')}@"
        url = f"{scheme}://{auth}{ep['host']}:{ep['port']}"
    else:
        url = f"{scheme}://{ep['host']}:{ep['port']}"
    return {"http": url, "https": url}, f"{ep['host']}:{ep['port']}"

def proxy_configured():
    return bool(os.getenv("kele_proxy_api", "").strip() or os.getenv("kele_proxy_static", "").strip())

def fetch_proxy():
    static = os.getenv("kele_proxy_static", "").strip()
    if static:
        ep = _parse_proxy_endpoint(static)
        if ep:
            return _proxy_dict(ep)
        return None, f"静态代理格式无法解析:{static}"
    
    api = os.getenv("kele_proxy_api", "").strip()
    if not api:
        return None, "未配置代理"
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36",
        "Accept": "*/*",
    }
    for i in range(PROXY_FETCH_RETRIES):
        try:
            resp = requests.get(api, headers=headers, timeout=15)
            raw = resp.text or ""
            print(f"📦 代理API原始返回(HTTP{resp.status_code}，{len(raw)}字符)：\n  {raw[:200]}")
            if resp.status_code == 200:
                ep = parse_proxy_response(raw)
                if ep:
                    mode = "账号密码" if ep.get("user") else "白名单"
                    print(f"✅ 解析成功：{ep['host']}:{ep['port']}（{mode}授权）")
                    return _proxy_dict(ep)
                print(f"⚠️ 代理响应无法解析(第{i+1}次)")
            else:
                print(f"⚠️ 代理API HTTP{resp.status_code}(第{i+1}次)")
        except Exception as e:
            print(f"⚠️ 代理API请求异常(第{i+1}次): {e}")
        if i < PROXY_FETCH_RETRIES - 1:
            time.sleep(2)
    return None, f"重试{PROXY_FETCH_RETRIES}次均失败"

# ==================== 业务逻辑 ====================
def parse_accounts(env_name):
    val = os.getenv(env_name, "").strip()
    if not val:
        return []
    # 兼容换行符、回车符、@、& 等常见多账号分隔符
    for sep in ["\n", "\r", "@", "&"]:
        val = val.replace(sep, ",")
    
    raw_list = [item.strip() for item in val.split(",") if item.strip()]
    accounts = []
    
    for idx, item in enumerate(raw_list, start=1):
        if "#" in item:
            parts = item.split("#", 1)
            remark = parts[0].strip()
            token = parts[1].strip()
        else:
            remark = f"账号 {idx}"
            token = item.strip()
        accounts.append((idx, remark, token))
        
    return accounts

def get_headers(auth_token):
    return {
        "Host": "m.eqq9td2yu8.cn",
        "Connection": "keep-alive",
        "sec-ch-ua-platform": '"Android"',
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36 XWEB/1500145 MMWEBSDK/20260801 MMWEBID/3410 REV/235faaae78aa6eb98ecd3e894bad1c03e48cfb9b MicroMessenger/8.0.78.3180(0x28004E32) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
        "Accept": "application/json, text/plain, */*",
        "udtauth40": auth_token,
        "Origin": "http://klld0913184327.eos-shanghai-1.cmecloud.cn",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "http://klld0913184327.eos-shanghai-1.cmecloud.cn/",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
    }

def query_user_info(session):
    try:
        res = session.get("https://m.eqq9td2yu8.cn/tuijian", timeout=10)
        data = res.json()
        if data.get("code") == 0:
            user = data.get("data", {}).get("user", {})
            name = user.get("username", "")
            uid = str(user.get("uid", ""))
            
            raw_score = float(user.get("score", "0"))
            total_gold = int(round(raw_score * 100))
            balance_yuan = round(total_gold / 10000.0, 3)
            
            summary = f"昵称: {name} (UID: {uid}) | 金币: {total_gold} 币，余额: {balance_yuan:.2f} 元"
            return balance_yuan, summary
    except Exception as e:
        print(f"⚠️ 查询账户信息异常: {e}")
    return 0.0, ""

def check_ali_bind_status(session):
    try:
        res = session.get("https://m.eqq9td2yu8.cn/withdrawal", timeout=10)
        data = res.json()
        if data.get("code") == 0:
            user = data.get("data", {}).get("user", {})
            ali_acc = user.get("u_ali_account")
            ali_name = user.get("u_ali_real_name")
            if ali_acc and ali_name:
                return True, f"已绑定支付宝: {ali_acc} ({ali_name})"
            return False, "未在平台绑定支付宝账号或姓名，请先前往提现界面手动绑定！"
    except Exception as e:
        return False, f"查询支付宝绑定信息异常: {e}"
    return False, "无法获取提现账户配置"

def do_withdraw(session, auth_token, current_balance, threshold_yuan, withdraw_type):
    if current_balance < threshold_yuan:
        print(f"💰 当前余额 {current_balance:.2f} 元，未达到提现门槛 {threshold_yuan:.2f} 元，跳过提现。")
        return False, "余额未达提现门槛"

    if withdraw_type == "ali":
        is_bound, bind_msg = check_ali_bind_status(session)
        if not is_bound:
            print(f"⚠️ 支付宝提现跳过: {bind_msg}")
            return False, bind_msg
        else:
            print(f"ℹ️ {bind_msg}")

    amount_param = int(round(current_balance * 100))
    if amount_param < 30:
        print("💰 提现金额小于平台最低限制（0.30元 / 3000币），跳过提现。")
        return False, "低于平台最低提现限制"

    withdraw_headers = get_headers(auth_token)
    withdraw_headers["Content-Type"] = "application/x-www-form-urlencoded"
    payload = {"amount": amount_param, "type": withdraw_type}
    type_name = "微信零钱" if withdraw_type == "wx" else "支付宝"

    try:
        print(f"💸 正在提交提现申请：提现 {current_balance:.2f} 元（扣除 {int(current_balance * 10000)} 币）到 {type_name}...")
        res = session.post("https://m.eqq9td2yu8.cn/withdrawal/doWithdraw", headers=withdraw_headers, data=payload, timeout=12)
        res_json = res.json()
        if res_json.get("code") == 0:
            msg = f"🎉 提现成功！已申请将 {current_balance:.2f} 元提现至 {type_name}"
            print(f"{msg}")
            return True, msg
        else:
            err_msg = res_json.get("msg", "未知失败原因")
            print(f"❌ 提现失败: {err_msg}")
            return False, f"提现失败: {err_msg}"
    except Exception as e:
        print(f"❌ 提现请求异常: {e}")
        return False, f"提现异常: {e}"

def get_new_read_iu(session):
    try:
        res = session.get("https://m.eqq9td2yu8.cn/new/bbbbb", timeout=12)
        data = res.json()
        jump_url = data.get("jump", "")
        if jump_url:
            m = re.search(r"iu=([a-zA-Z0-9_-]+)", jump_url)
            if m:
                return m.group(1)
            print(f"❌ 未从返回链接中解析出 iu 参数: {jump_url}")
        else:
            print(f"❌ 主站未返回跳转链接: {res.text}")
    except Exception as e:
        print(f"❌ 申请最新任务链接异常: {e}")
    return ""

def prompt_user_read_check_article(article_url, remark, current_round):
    print(f"🚨 读到第 {current_round} 篇，正在截获微信文章链接推送...")
    push_title = f"可乐阅读【{remark}】文章链接"
    push_content = (
        f"账号【{remark}】读到第 {current_round} 篇！\n"
        f"请在 50 秒内，在手机微信中点开下方文章链接，正常浏览 6~8 秒：\n\n"
        f"{article_url}\n\n"
        f"（若超时未操作，脚本将安全退出）"
    )
    notify_safe(push_title, push_content)

    print("⏳ 已启动 50 秒等待。请在手机微信中点开阅读并停留 6 秒...")
    start_t = time.time()
    while time.time() - start_t < 50:
        remain = int(50 - (time.time() - start_t))
        if remain > 0 and remain % 10 == 0:
            print(f"⏳ 等待微信手动阅读中... 剩余 {remain} 秒")
        time.sleep(2)

    print("⏱️ 倒计时结束，继续进行后续结算。")
    return True

def run_account(account_idx, remark, auth_token, max_read):
    set_account_tag(remark)
    print(f"================ 开始执行 ================")
    session = requests.Session()
    session.headers.update(get_headers(auth_token))

    use_proxy = proxy_configured()
    if use_proxy:
        print("🔗 直连试跑，首次结算金币成功后拉取代理原地切换（节省代理IP）")

    init_balance, summary = query_user_info(session)
    if summary:
        print(f"📋 执行前账户数据: {summary}")

    print("正在向主站请求派发最新阅读任务...")
    iu = get_new_read_iu(session)
    if not iu:
        print("❌ 无法获取当轮任务，请检查账号是否处于24小时风控期或 Token 是否失效。")
        return account_idx, remark, 0, "任务获取失败（疑似处于24小时风控期）", ""

    print(f"✅ 成功获取本轮任务身份凭证: {iu[:15]}...")

    jkey = ""
    success_count = 0
    continuous_err = 0
    proxy_switched = False
    proxy_refreshes = 0
    proxy_cooldown_until = 0

    for i in range(1, max_read + 1):
        if continuous_err >= 3:
            if proxy_switched and proxy_refreshes < 2:
                proxy_refreshes += 1
                print(f"🔄 疑似代理失效，重拉新 IP 续命（第{proxy_refreshes}/2次）...")
                new_proxies, new_info = fetch_proxy()
                if new_proxies:
                    session.proxies.update(new_proxies)
                    continuous_err = 0
                    print(f"✅ 已切换新代理: {new_info}，继续执行")
                    continue
            print("❌ 连续异常达到 3 次，终止本账号当前轮次。")
            break

        params = {"iu": iu, "pageshow": "", "r": str(random.random())}
        if jkey:
            params["jkey"] = jkey

        try:
            res = session.get("https://m.eqq9td2yu8.cn/dodoaa/mmaa", params=params, timeout=15)
            data = res.json()
        except Exception as e:
            print(f"❌ 网络请求异常或返回非JSON格式: {e}")
            continuous_err += 1
            time.sleep(3)
            continue

        msg_str = str(data.get("success_msg", "") or data.get("msg", ""))
        article_url = data.get("url", "")
        if "检测未通过" in msg_str or data.get("check_finish") == 1 or article_url == "close":
            print(f"⚠️ 平台提示链接已关闭: {data}")
            print(f"🛡️ 脚本主动静默退出，保护账号不被追加封禁！")
            break

        if "jkey" in data:
            jkey = data.get("jkey", "")
            article_url = data.get("url", "")
            msg = data.get("success_msg", "")
            continuous_err = 0

            if msg:
                print(f"🎉 [{i}/{max_read}] 结算成功: {msg}")
                success_count += 1
            else:
                print(f"📖 [{i}/{max_read}] 任务下发成功，已取得文章链接")

            if i in [2, 3] and article_url and "mp.weixin.qq.com" in article_url:
                prompt_user_read_check_article(article_url, remark, i)

            if use_proxy and not proxy_switched:
                if time.time() < proxy_cooldown_until:
                    print("⏳ 代理API冷却中，本篇继续直连")
                else:
                    proxies, info = fetch_proxy()
                    if proxies:
                        session.proxies.update(proxies)
                        proxy_switched = True
                        print(f"✅ 结算成功，已原地切换代理: {info}（后续请求走代理通道）")
                    else:
                        print(f"⚠️ 代理拉取失败: {info}，继续直连（下篇结算再试）")
                        proxy_cooldown_until = time.time() + 60

            if article_url and "mp.weixin.qq.com" in article_url:
                try:
                    session.get(article_url, timeout=10)
                except Exception:
                    pass

            sleep_sec = round(random.uniform(MIN_WAIT_SECONDS, MAX_WAIT_SECONDS), 1)
            time.sleep(sleep_sec)

        elif "url" in data and "error.html" in data["url"]:
            print(f"❌ 任务结束或链接失效: {data['url']}")
            break
        elif "msg" in data or "error" in data:
            print(f"⚠️ 平台提示: {data.get('msg') or data.get('error')}")
            break
        else:
            print(f"⚠️ 返回未知内容: {res.text[:120]}")
            continuous_err += 1
            time.sleep(3)

    final_balance, updated_summary = query_user_info(session)
    if updated_summary:
        print(f"📊 结算后账户数据 -> {updated_summary}")

    withdraw_info = ""
    if AUTO_WITHDRAW:
        success, withdraw_info = do_withdraw(session, auth_token, final_balance, WITHDRAW_THRESHOLD, WITHDRAW_TYPE)
        if success:
            _, after_withdraw_summary = query_user_info(session)
            if after_withdraw_summary:
                updated_summary = after_withdraw_summary

    session.close()
    print(f"================ 完毕：共完成 {success_count} 篇 ================")
    return account_idx, remark, success_count, (updated_summary if updated_summary else summary), withdraw_info

def main():
    account_list = parse_accounts("kele_auth")
    if not account_list:
        print("❌ 未找到环境变量: kele_auth")
        print("请在青龙面板添加环境变量 kele_auth，值填入抓包得到的 udtauth40 字符串。")
        print("支持格式：大号#token1@小号#token2 或使用换行/&分隔")
        sys.exit(1)

    print(f"👥 共检测到 {len(account_list)} 个账号配置")
    for idx, remark, _ in account_list:
        print(f"  ├─ 序号 {idx}: 【{remark}】")

    reports = ["📖 可乐阅读执行报告"]
    total = 0
    results = []

    if ENABLE_CONCURRENT and len(account_list) > 1:
        print(f"🚀 已开启多账号并发模式，最大并发数: {MAX_WORKERS}")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(run_account, idx, remark, auth, READ_NUM) for idx, remark, auth in account_list]
            for future in as_completed(futures):
                try:
                    res = future.result()
                    results.append(res)
                except Exception as e:
                    print(f"❌ 线程执行异常: {e}")
        results.sort(key=lambda x: x[0])
    else:
        print("🐢 当前为顺序执行模式")
        for idx, remark, auth in account_list:
            res = run_account(idx, remark, auth, READ_NUM)
            results.append(res)

    for idx, remark, done, summary, withdraw_res in results:
        total += done
        account_report = f"【{remark}】: 成功完成 {done} 篇 | {summary}"
        if withdraw_res:
            account_report += f"\n  └─ 提现状态: {withdraw_res}"
        reports.append(account_report)

    reports.append(f"\n📊 本轮合计完成: {total} 篇")
    notify_safe("可乐阅读执行通知", "\n".join(reports))

if __name__ == "__main__":
    main()
