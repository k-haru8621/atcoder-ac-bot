import discord
from discord import app_commands
from discord.ext import tasks
import os, aiohttp, re, gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
from bs4 import BeautifulSoup

# --- Flask Server ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running!"
def run(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run).start()

# --- 設定 ---
JST = timezone(timedelta(hours=9))
SHEET_NAME = "AtCoderBot_DB"

EMOJI_MAP = {
    "AC": "<:atcoder_bot_AC:1463065663429021917>",
    "WA": "<:atcoder_bot_WA:1463065707703959643>",
    "TLE": "<:atcoder_bot_TLE:1463065790256382086>",
    "RE": "<:atcoder_bot_RE:1463065747705172165>",
    "CE": "<:atcoder_bot_CE:1463065865561051228>",
    "MLE": "<:atcoder_bot_MLE:1463065831763349514>"
}


class AtCoderBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.user_data = {}
        self.news_config = {}
        self.problems_map = {}
        self.diff_map = {}
        self.sent_notifications = set()
        
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open(SHEET_NAME)
        except Exception as e: print(f"⚠️ Sheetsエラー: {e}")
            
    def get_rated_color(self, rated_str):
        """レーティング上限に基づいた色を返す"""
        if not rated_str or "Unrated" in rated_str:
            return 0x808080  # 灰色
        if "All" in rated_str:
            return 0xFF0000  # 赤
        
        # 「~ 1999」から 1999 を抽出
        match = re.search(r'(\d+)', rated_str)
        if not match: return 0x808080
        
        val = int(match.group(1))
        if val < 1200: return 0x008000 # 緑
        if val < 2000: return 0x0000FF # 青
        if val < 2800: return 0xFF8000 # 橙
        return 0xFF0000 # 赤

    def save_to_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            ws_user.clear()
            # ヘッダーを書き込む
            ws_user.append_row(["GuildID", "AtCoderID", "DiscordID", "ChannelID", "OnlyAC", "LastSubID"])
            
            rows = []
            for key, v in self.user_data.items():
                # self.user_data の中身を1行ずつリストにする
                rows.append([
                    str(v['guild_id']), 
                    v['atcoder_id'], 
                    str(v['discord_user_id']), 
                    str(v['channel_id']), 
                    str(v['only_ac']), 
                    str(v.get('last_sub_id', 0))
                ])
            
            if rows:
                ws_user.append_rows(rows) # まとめてスプレッドシートへ
        except Exception as e:
            print(f"❌ 書き込み失敗: {e}")

    def load_from_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            for r in ws_user.get_all_records():
                # 「サーバーID_ユーザー名」で固有の鍵を作る
                gid = str(r['GuildID'])
                aid = r['AtCoderID']
                key = f"{gid}_{aid}"
                
                self.user_data[key] = {
                    "guild_id": int(gid),
                    "atcoder_id": aid,
                    "discord_user_id": int(r['DiscordID']),
                    "channel_id": int(r['ChannelID']),
                    "only_ac": str(r['OnlyAC']).lower() == 'true',
                    "last_sub_id": int(r.get('LastSubID', 0))
                }
        except Exception as e:
            print(f"❌ 読み込み失敗: {e}")
            
    async def setup_hook(self):
        self.load_from_sheets()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://kenkoooo.com/atcoder/resources/problems.json") as r:
                    if r.status == 200: self.problems_map = {x['id']: x['title'] for x in await r.json()}
                async with session.get("https://kenkoooo.com/atcoder/resources/problem-models.json") as r:
                    if r.status == 200: self.diff_map = await r.json()
        except: pass
        self.check_submissions.start()
        self.auto_contest_scheduler.start()
        await self.tree.sync()

    # --- AtCoderBotクラス内に追加 ---
    async def fetch_user_data(self, session, atcoder_id):
        profile_url = f"https://atcoder.jp/users/{atcoder_id}?lang=ja"
        history_url = f"https://atcoder.jp/users/{atcoder_id}/history/json"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        
        data = {
            "atcoder_id": atcoder_id, "rating": 0, "max_rating": "---", 
            "diff": "---", "birth": "---", "org": "---", 
            "last_date": "---", "last_contest": "---", "last_contest_url": "",
            "contest_count": "---", "last_rank": "---", "rank_all": "---", "history": []
        }

        try:
            # 1. コンテスト履歴 (JSON) を先に取得して最新レートと個別順位を確定
            async with session.get(history_url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    h_json = await resp.json()
                    rated_only = [h for h in h_json if h.get('IsRated')]
                    if rated_only:
                        latest_5 = rated_only[::-1][:5]
                        for i, h in enumerate(latest_5):
                            dt = datetime.fromisoformat(h['EndTime']).astimezone(JST)
                            full_name = h.get('ContestName', 'Unknown')
                            c_id = h.get('ContestScreenName', '').split('.')[0]
                            
                            # コンテスト名の略称ルール (ABC/ARC/AGC/AHC)
                            import re
                            if "Beginner Contest" in full_name: name = f"ABC{re.search(r'\d+', full_name).group()}"
                            elif "Regular Contest" in full_name: name = f"ARC{re.search(r'\d+', full_name).group()}"
                            elif "Grand Contest" in full_name: name = f"AGC{re.search(r'\d+', full_name).group()}"
                            elif "Heuristic Contest" in full_name: name = f"AHC{re.search(r'\d+', full_name).group()}"
                            else: name = full_name[:10]

                            data["history"].append({
                                "name": name,
                                "date": dt.strftime('%m/%d'),
                                "perf": h.get('Performance', '---'),
                                "rate": h.get('NewRating', '---'),
                                "rank": h.get('Place', '---'),
                                "url": f"https://atcoder.jp/contests/{c_id}/standings?watching={atcoder_id}"
                            })
                            
                            if i == 0:
                                data["rating"] = h.get('NewRating', 0)
                                data["last_rank"] = h.get('Place', '---')
                                data["last_date"] = dt.strftime('%Y/%m/%d')
                                data["last_contest"] = full_name
                                data["last_contest_url"] = f"https://atcoder.jp/contests/{c_id}"
                                if len(rated_only) >= 2:
                                    change = h['NewRating'] - rated_only[-2]['NewRating']
                                    data["diff"] = f"{'+' if change > 0 else ''}{change}"

            # 2. プロフィールページの解析 (順位 5486th と 最高レート 1495 を取得)
            async with session.get(profile_url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    soup = BeautifulSoup(await resp.text(), 'html.parser')
                    for t in soup.find_all('table', class_='dl-table'):
                        for row in t.find_all('tr'):
                            th = row.find('th')
                            td = row.find('td')
                            if th and td:
                                label = th.get_text(strip=True)
                                # ★重要: get_text(" ") でタグ間にスペースを入れ、数字の合体を防ぐ
                                val = td.get_text(" ", strip=True).replace('―', '').strip()
                                
                                if "順位" in label and "位" not in label:
                                    data["rank_all"] = val # ここで 5486th を取得
                                if "誕生年" in label: data["birth"] = val
                                if "所属" in label: data["org"] = val
                                if "コンテスト参加回数" in label: data["contest_count"] = val
                                
                                if "Rating最高値" in label:
                                    if val != "---":
                                        import re
                                        parts = val.split()
                                        if parts:
                                            # 最初の塊が 1495、それ以降が級や昇格情報
                                            max_r = parts[0]
                                            detail = " ".join(parts[1:])
                                            data["max_rating"] = f"{max_r} ({detail})"
                                    else:
                                        data["max_rating"] = "---"
            return data
        except Exception as e:
            print(f"Fetch Error: {e}")
            return None
            
    @tasks.loop(minutes=3)
    async def check_submissions(self):
        # セッションをループの外で作成（効率化）
        async with aiohttp.ClientSession() as session:
            # 辞書のコピーに対してループを回す（実行中のサイズ変更エラー防止）
            for key in list(self.user_data.keys()):
                info = self.user_data[key]
                try:
                    await self.process_submissions(session, info, lookback_seconds=259200)
                except Exception as e:
                    print(f"⚠️ 提出確認エラー ({key}): {e}")

    async def process_submissions(self, session, info, lookback_seconds):
        atcoder_id = info['atcoder_id']
        guild_id = info['guild_id']
        key = f"{guild_id}_{atcoder_id}"
        
        # 過去の保存データから最後に通知したIDを取得
        last_id = int(info.get('last_sub_id', 0))
        
        # 2日分（172800秒）遡って取得するようにURLを作成
        # 引数の lookback_seconds が 172800 (2日) であることを想定
        url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={int(datetime.now().timestamp() - lookback_seconds)}"
        
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    subs = await resp.json()
                    if not subs:
                        return

                    new_last_id = last_id
                    # 提出を古い順（ID昇順）に並べる
                    sorted_subs = sorted(subs, key=lambda x: x['id'])

                    for sub in sorted_subs:
                        # 既に通知済みのIDなら飛ばす（2回目以降のループ用）
                        if last_id != 0 and sub['id'] <= last_id:
                            continue
                        
                        # ACのみ通知の設定なら、AC以外を飛ばす
                        if info.get('only_ac', True) and sub['result'] != 'AC':
                            new_last_id = max(new_last_id, sub['id'])
                            continue
                        
                        # 通知送信！
                        # (登録直後なら、ここで過去2日分の通知が連続で飛びます)
                        await self.send_ac_notification(info, sub)
                        
                        # 通知した中で最新のIDを保持
                        new_last_id = max(new_last_id, sub['id'])
                    
                    # 最後にまとめて「どこまで通知したか」を保存
                    if new_last_id > last_id:
                        self.user_data[key]['last_sub_id'] = new_last_id
                        self.save_to_sheets()
        except Exception as e:
            print(f"⚠️ process_submissions エラー ({key}): {e}")
            
    async def send_ac_notification(self, info, sub):
        channel = self.get_channel(info['channel_id'])
        if not channel: return
        prob_id, atcoder_id = sub['problem_id'], info['atcoder_id']
        prob_title = self.problems_map.get(prob_id, prob_id)
        difficulty = self.diff_map.get(prob_id, {}).get('difficulty')
        user = self.get_user(info['discord_user_id'])
        user_name = user.display_name if user else "Unknown"
        user_icon = user.display_avatar.url if user else None
        res = sub['result']
        emoji = EMOJI_MAP.get(res, "❓")
        def get_color(d):
            if d is None: return 0x808080
            colors = [(400, 0x808080), (800, 0x804000), (1200, 0x008000), (1600, 0x00C0C0), (2000, 0x0000FF), (2400, 0xFFFF00), (2800, 0xFF8000)]
            for limit, color in colors:
                if d < limit: return color
            return 0xFF0000
        embed = discord.Embed(title=prob_title, url=f"https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id}", color=get_color(difficulty))
        embed.set_author(name=f"{user_name}", icon_url=user_icon)
        exec_time = sub.get('execution_time') or 0
        desc = (f"user : [{atcoder_id}](https://atcoder.jp/users/{atcoder_id}) / result : {emoji} **[{res}]**\n"
                f"difficulty : {difficulty if difficulty is not None else '---'} / {exec_time}ms / score : {int(sub['point'])}\n"
                f"language : {sub['language']}\n\n"
                f"📄 [{atcoder_id}さんの提出を見る](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})\n"
                f"🔍 [解説を読む](https://atcoder.jp/contests/{sub['contest_id']}/editorial)")
        embed.description = desc
        dt = datetime.fromtimestamp(sub['epoch_second'], JST)
        embed.set_footer(text=f"提出時刻 : {dt.strftime('%b %d, %Y (%a) %H:%M:%S')}")
        await channel.send(embed=embed)

    async def fetch_recent_announcements(self, session):
        results = {}
        try:
            # 日本語ページを強制
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                soup = BeautifulSoup(await resp.text(), 'html.parser')
            
            for post in soup.find_all('div', class_='panel-default'):
                body = post.find('div', class_='panel-body blog-post')
                if not body: continue
                
                # コンテストURLの取得と正規化
                link_tag = body.find('a', href=re.compile(r'https://atcoder\.jp/contests/[^" \n]+'))
                if not link_tag: continue
                c_url = link_tag['href'].split('?')[0].rstrip('/')
                
                info = {"writer": "不明", "tester": "不明", "points": "未発表"}

                # 名前を抽出する専用ロジック (aタグの中身を拾う)
                def extract_users(keyword):
                    target = body.find(string=re.compile(keyword))
                    if not target: return None
                    # キーワードの親要素から /users/ リンクを持つaタグをすべて取得
                    links = target.parent.find_all('a', href=re.compile(r'/users/'))
                    return ", ".join([u.get_text(strip=True) for u in links]) if links else None

                info["writer"] = extract_users("Writer") or "不明"
                info["tester"] = extract_users("Tester") or "不明"

                # 配点のパース (テキストから取得)
                content_text = body.get_text("|", strip=True)
                for line in content_text.split("|"):
                    if "配点：" in line or "配点:" in line:
                        info["points"] = line.split("：")[-1].split(":")[-1].strip()
                
                results[c_url] = info
        except Exception as e:
            print(f"⚠️ 告知解析エラー: {e}")
        return results
        
    async def broadcast_contest(self, name, url, st, dur, rated, label, details, is_10min=False, is_start=False, is_end=False):
        # 終了通知(cend)の場合もユニークキーを作って二重送信防止
        key = f"{label}_{url}"
        if key in self.sent_notifications: return
        self.sent_notifications.add(key)
        embed = self.create_contest_embed(name, url, st, dur, rated, details, is_10min, is_start, is_end)
        for cid in self.news_config.values():
            channel = self.get_channel(cid)
            if channel: await channel.send(content=f"**{label}**", embed=embed)

    def create_contest_embed(self, name, url, st, dur_min, rated, details, is_start=False):
        # self.get_rated_color を呼び出すように変更
        color = self.get_rated_color(rated)
        embed = discord.Embed(title=name, url=url, color=color)
        unix_time = int(st.timestamp())

        if is_start:
            embed.description = f"🚀 **開始しました！**\n\n📈 [順位表]({url}/standings)\n📄 [解説]({url}/editorial)"
        else:
            embed.description = (
                f"**コンテストページ：** {url}\n"
                f"**開始時刻：** {st.strftime('%Y-%m-%d %H:%M')}\n"
                f"**コンテスト時間：** {dur_min} 分\n"
                f"**Writer：** {details.get('writer', '不明')}\n"
                f"**Tester：** {details.get('tester', '不明')}\n"
                f"**レーティング変化：** {rated}\n"
                f"**配点：** {details.get('points', '未発表')}\n"
                f"**コンテスト開始まで：** <t:{unix_time}:R>"
            )
        embed.set_footer(text=f"AtCoder - {st.strftime('%Y/%m/%d')}")
        return embed
        
    async def check_immediate_announcement(self, channel_id):
        now = datetime.now(JST)
        channel = self.get_channel(channel_id)
        if not channel: return
        
        status_msg = await channel.send(f"⏳ 最終デプロイ確認中... ({now.strftime('%H:%M:%S')})")
        async with aiohttp.ClientSession() as session:
            recent_details = await self.fetch_recent_announcements(session)
            
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                # 予定テーブル
                table = soup.find('div', id='contest-table-upcoming')
                if not table: return

                rows = table.find_all('tr')[1:]
                log_txt = "📊 **最終解析結果**\n```\n"
                found_any = False

                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) < 2: continue
                    
                    time_tag = row.find('time')
                    a_tag = cols[1].find('a')
                    if not time_tag or not a_tag: continue

                    c_url = "[https://atcoder.jp](https://atcoder.jp)" + a_tag['href'].split('?')[0].rstrip('/')
                    c_name = a_tag.text.strip()
                    
                    try:
                        st_dt = datetime.strptime(time_tag.text.strip(), '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                        diff = int((st_dt - now).total_seconds() / 60)

                        if 0 < diff <= 1440:
                            # 取得した本質データと合体
                            info = recent_details.get(c_url, {"writer":"確認中","tester":"確認中","points":"確認中"})
                            
                            # Embed送信で失敗してもループを止めないガード
                            try:
                                # 列の存在チェックを厳密に
                                duration = cols[2].text.strip() if len(cols) > 2 else "不明"
                                rated = cols[3].text.strip() if len(cols) > 3 else "不明"
                                
                                await self.broadcast_contest(c_name, c_url, st_dt, duration, rated, "⏰ 本日開催", info)
                                log_txt += f"・{c_name[:12]} | ✅ 送信成功\n"
                                found_any = True
                            except Exception as discord_e:
                                log_txt += f"・{c_name[:12]} | ❌ 400エラー: {str(discord_e)[:10]}\n"
                        else:
                            log_txt += f"・{c_name[:12]} | {diff}分前\n"
                    except: continue

                log_txt += "```"
                await status_msg.edit(content=log_txt)
                
    @tasks.loop(minutes=1)
    async def auto_contest_scheduler(self):
        # 現在時刻を1分単位で取得
        now = datetime.now(JST).replace(second=0, microsecond=0)
        
        async with aiohttp.ClientSession() as session:
            # 1. まず告知パネルから Writer/Tester/配点 情報を取得
            recent_details = await self.fetch_recent_announcements(session)
            
            # 2. トップページ（日本語）を取得してテーブルを解析
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200: return
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                
                # 「今後の予定」と「開催中」のテーブル両方をチェック
                for table_id in ['contest-table-upcoming', 'contest-table-active']:
                    container = soup.find('div', id=table_id)
                    if not container: continue
                    
                    for row in container.find_all('tr')[1:]: # ヘッダーを飛ばす
                        cols = row.find_all('td')
                        if len(cols) < 4: continue
                        
                        try:
                            # --- 時刻と時間の解析 ---
                            time_tag = cols[0].find('time')
                            if not time_tag: continue
                            time_str = time_tag.text
                            # 曜日(Sat)などを除去してパース
                            clean_time = re.sub(r'\(.*?\)', '', time_str).strip()
                            st_dt = datetime.strptime(clean_time, '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                            
                            # コンテスト時間（例: 01:40 -> 100分）を計算
                            dur_str = cols[2].text.strip()
                            h, m = map(int, dur_str.split(':'))
                            duration_min = h * 60 + m
                            en_dt = st_dt + timedelta(minutes=duration_min)
                            
                            # --- URLと詳細情報の紐付け ---
                            name_tag = cols[1].find('a')
                            if not name_tag: continue
                            # URLを正規化（末尾のスラッシュを削除して一致率を上げる）
                            raw_path = name_tag['href'].split('?')[0].rstrip('/')
                            c_url = f"https://atcoder.jp{raw_path}"
                            
                            # 告知パネルから取った詳細を合体（なければ不明を入れる）
                            details = recent_details.get(c_url, {"writer": "不明", "tester": "不明", "points": "未発表"})
                            
                            # --- 通知判定 ---
                            diff_st = round((st_dt - now).total_seconds() / 60) # 開始まで
                            diff_en = round((en_dt - now).total_seconds() / 60) # 終了まで
                            rated = cols[3].text.strip() # レーティング対象範囲
                            
                            # 24時間前
                            if diff_st == 1440:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, duration_min, rated, "⏰ 24時間前", details)
                            
                            # 30分前
                            elif diff_st == 30:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, duration_min, rated, "⚠️ 30分前", details)
                            
                            # 開始
                            elif diff_st == 0:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, duration_min, rated, "🚀 開始！", details, is_start=True)
                            
                            # 終了
                            elif diff_en == 0:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, duration_min, rated, "🏁 終了！", details)

                        except Exception as e:
                            # 1つの行でエラーが出ても他の行の処理を続ける
                            continue

bot = AtCoderBot()

@bot.tree.command(name="register", description="提出通知の登録")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    try: await interaction.response.defer()
    except: return
    info = {"guild_id": interaction.guild_id, "discord_user_id": discord_user.id, "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac, "last_sub_id": 0}
    bot.user_data[f"{interaction.guild_id}_{atcoder_id}"] = info
    bot.save_to_sheets()
    await interaction.followup.send(f"✅ `{atcoder_id}` 登録完了。")
    async with aiohttp.ClientSession() as session: await bot.process_submissions(session, info, lookback_seconds=86400)

@bot.tree.command(name="delete", description="提出通知の削除")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    try: await interaction.response.defer()
    except: return
    key = f"{interaction.guild_id}_{atcoder_id}"
    if key in bot.user_data:
        del bot.user_data[key]; bot.save_to_sheets()
        await interaction.followup.send(f"🗑️ `{atcoder_id}` 削除。")
    else: await interaction.followup.send("未登録です。")

@bot.tree.command(name="notice_set", description="告知チャンネル設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    try: await interaction.response.defer()
    except: return
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_to_sheets()
    # 最初に「考え中」を消すための応答を返す
    await interaction.response.send_message(f"告知先を {channel.mention} に設定しました。", ephemeral=True)
    
    # その後に重たい処理（check_immediate_announcement）を実行する
    await bot.check_immediate_announcement(channel.id)

@bot.tree.command(name="notice_delete", description="告知削除")
async def notice_delete(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except: return
    gid = str(interaction.guild_id)
    if gid in bot.news_config:
        del bot.news_config[gid]; bot.save_to_sheets()
        await interaction.followup.send("🗑️ 告知削除。")
    else: await interaction.followup.send("未設定。")

# --- コマンドセクションに追加 ---
@bot.tree.command(name="status", description="AtCoderステータスを表示")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    
    # ユーザー紐付け確認
    atcoder_id = next((v['atcoder_id'] for v in bot.user_data.values() if v['discord_user_id'] == target.id), None)
    if not atcoder_id:
        return await interaction.followup.send(f"❌ {target.name} さんのIDが登録されていません。")

    async with aiohttp.ClientSession() as session:
        d = await bot.fetch_user_data(session, atcoder_id)

    # ここが重要：d が辞書（dict）でない場合はエラーとして処理する
    if not isinstance(d, dict):
        error_text = "データ取得失敗"
        if isinstance(d, str):
            if "PROFILE_NOT_FOUND" in d:
                error_text = f"ユーザー `{atcoder_id}` が見つかりませんでした。IDが正しいか確認してください。"
            elif "HISTORY_NOT_FOUND" in d:
                error_text = f"`{atcoder_id}` さんのコンテスト履歴が取得できませんでした。"
            else:
                error_text = f"エラーが発生しました: `{d}`"
        
        return await interaction.followup.send(f"❌ {error_text}")

    # 色判定
    def get_color(r):
        colors = [(2800, 0xFF0000), (2400, 0xFF8000), (2000, 0xFFFF00), (1600, 0x0000FF), (1200, 0x00C0C0), (800, 0x008000), (400, 0x804000)]
        for threshold, color in colors:
            if r >= threshold: return color
        return 0x808080

    # フッター用日時（曜日付き）
    wd_ja = ["月", "火", "水", "木", "金", "土", "日"]
    now = datetime.now(JST)
    date_str = now.strftime(f'%Y年%m月%d日({wd_ja[now.weekday()]}) %H:%M')

    embed = discord.Embed(color=get_color(d["rating"]))
    
    # 【変更点】ヘッダーにAtCoderリンクを重ねる
    embed.set_author(
        name=f"{target.name} / {d['atcoder_id']}", 
        url=f"https://atcoder.jp/users/{d['atcoder_id']}", 
        icon_url=target.display_avatar.url
    )

    embed.add_field(
        name="📊 現在のステータス",
        value=(f"**現在のレーティング:** `{d['rating']}` (前回比: {d['diff']})\n"
               f"**最高レーティング:** `{d['max_rating']}`\n"
               f"**出場数:** {d['contest_count']} 回 / **所属:** {d['org']}\n"
               f"**誕生年:** {d['birth']}\n"
               f"**最終参加:** {d['last_date']}\n└ *{d['last_contest']}*"),
        inline=False
    )

    if d["history"]:
        h_lines = [f"**{h['name']}** ({h['date']}) パフォーマンス: **{h['perf']}** → 新レート: **{h['rate']}**" for h in d["history"]]
        embed.add_field(name="🏆 直近のコンテスト成績", value="\n".join(h_lines), inline=False)

    # 【変更点】フッターに日時と曜日
    embed.set_footer(text=f"{date_str} 時点")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="preview", description="各種通知のプレビュー")
@app_commands.choices(type=[
    app_commands.Choice(name="提出通知", value="ac"),
    app_commands.Choice(name="24時間前", value="c24"),
    app_commands.Choice(name="30分前", value="c30"),
    app_commands.Choice(name="開始", value="cstart"),
    app_commands.Choice(name="終了", value="cend")
])
async def preview(interaction: discord.Interaction, type: str):
    try: await interaction.response.defer(ephemeral=True)
    except: return
    dummy_details = {"writer": "Staff", "tester": "Tester", "points": "100-200-300"}
    dummy_url = "https://atcoder.jp/contests/practice"
    dummy_st = datetime.now(JST)
    if type == "ac":
        await bot.send_ac_notification({'atcoder_id': 'atcoder', 'discord_user_id': interaction.user.id, 'channel_id': interaction.channel_id}, {'id': 0, 'problem_id': 'abc_a', 'contest_id': 'abc', 'result': 'AC', 'point': 100, 'language': 'Python', 'epoch_second': int(datetime.now().timestamp())})
    else:
        # 時間を "01:40" (文字列) から 100 (数値) に変更
        # かつ、不要な引数 (is_10min等) を削除
        if type == "c24": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, 100, "All", dummy_details)
        elif type == "c30": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, 100, "All", dummy_details)
        elif type == "cstart": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, 100, "All", dummy_details, is_start=True)
        elif type == "cend": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, 100, "All", dummy_details)
        msg = f"**Preview: {type}**"
        
    # 既に一度 response を使っている場合は followup を使う
    await interaction.followup.send(content=f"**Preview: {type}**", embed=e)

if __name__ == "__main__":
    keep_alive(); bot.run(os.getenv("DISCORD_TOKEN"))
