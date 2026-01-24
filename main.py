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

def get_rated_color(rating_str):
    if "All" in rating_str: return 0xFF0000 
    match = re.search(r'(\d+)', rating_str)
    if not match: return 0x000000 
    val = int(match.group(1))
    if val < 1200: return 0x008000
    if val < 2000: return 0x0000FF
    if val < 2800: return 0xFF8000
    return 0xFF0000

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

    def save_to_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            ws_user.clear()
            ws_user.append_row(["GuildID", "AtCoderID", "DiscordID", "ChannelID", "OnlyAC", "LastSubID"])
            rows = [[str(v['guild_id']), v['atcoder_id'], str(v['discord_user_id']), str(v['channel_id']), str(v['only_ac']), str(v.get('last_sub_id', 0))] for v in self.user_data.values()]
            if rows: ws_user.append_rows(rows)
            ws_config = self.sheet.worksheet("config")
            ws_config.clear()
            ws_config.append_row(["GuildID", "ChannelID"])
            rows_config = [[str(gid), str(cid)] for gid, cid in self.news_config.items()]
            if rows_config: ws_config.append_rows(rows_config)
        except Exception as e: print(f"❌ 書き込み失敗: {e}")

    def load_from_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            for r in ws_user.get_all_records():
                key = f"{r['GuildID']}_{r['AtCoderID']}"
                self.user_data[key] = {"guild_id": int(r['GuildID']), "atcoder_id": r['AtCoderID'], "discord_user_id": int(r['DiscordID']), "channel_id": int(r['ChannelID']), "only_ac": str(r['OnlyAC']).lower() == 'true', "last_sub_id": int(r.get('LastSubID', 0))}
            ws_config = self.sheet.worksheet("config")
            for r in ws_config.get_all_records(): self.news_config[str(r['GuildID'])] = int(r['ChannelID'])
        except Exception as e: print(f"❌ 読み込み失敗: {e}")

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

    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for key, info in list(self.user_data.items()):
                await self.process_submissions(session, info, lookback_seconds=259200)

    async def process_submissions(self, session, info, lookback_seconds):
        atcoder_id = info['atcoder_id']
        last_id = info.get('last_sub_id', 0)
        url = f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={atcoder_id}&from_second={int(datetime.now().timestamp() - lookback_seconds)}"
        async with session.get(url) as resp:
            if resp.status == 200:
                subs = await resp.json()
                new_last_id = last_id
                for sub in sorted(subs, key=lambda x: x['id']):
                    if sub['id'] <= last_id: continue
                    if info.get('only_ac', True) and sub['result'] != 'AC': continue
                    await self.send_ac_notification(info, sub)
                    new_last_id = max(new_last_id, sub['id'])
                if new_last_id > last_id:
                    self.user_data[f"{info['guild_id']}_{atcoder_id}"]['last_sub_id'] = new_last_id
                    self.save_to_sheets()

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

    async def fetch_recent_announcements(self, session, log_channel=None):
        results = {}
        try:
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                home_source = await resp.text()
            post_ids = re.findall(r'href="/posts/(\d+)"', home_source)
            unique_posts = sorted(list(set(post_ids)), reverse=True)[:15]
            for pid in unique_posts:
                url = f"https://atcoder.jp/posts/{pid}"
                async with session.get(url) as resp:
                    if resp.status != 200: continue
                    raw = await resp.text()
                    c_match = re.search(r'https://atcoder\.jp/contests/([a-zA-Z0-9_-]+)', raw)
                    if not c_match: continue
                    c_url = c_match.group(0).rstrip('/')
                    details = {"writer": "不明", "tester": "不明", "points": "不明"}
                    w_m = re.search(r"Writer[:：]\s*(?:<[^>]+>)*\s*([^<\n]+)", raw)
                    if w_m: details["writer"] = re.sub(r'<[^>]*>', '', w_m.group(1)).strip()
                    t_m = re.search(r"Tester[:：]\s*(?:<[^>]+>)*\s*([^<\n]+)", raw)
                    if t_m: details["tester"] = re.sub(r'<[^>]*>', '', t_m.group(1)).strip()
                    p_m = re.search(r"(?:配点|Score)[は：\s]*([0-9\-\s/点]+)", raw)
                    if p_m: details["points"] = p_m.group(1).strip()
                    results[c_url] = details
            if log_channel: await log_channel.send(f"✅ 解析エンジン完了: {len(results)}件取得")
        except Exception as e:
            if log_channel: await log_channel.send(f"⚠️ 解析エンジンエラー: {e}")
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

    def create_contest_embed(self, name, url, st, dur, rated, details, is_10min=False, is_start=False, is_end=False):
        embed = discord.Embed(title=name, url=url, color=get_rated_color(rated))
        if is_10min:
            embed.description = f"コンテストまで残り30分！\n👉 [参加登録]({url})\n配点： {details['points']}"
        elif is_start:
            try:
                h, m = map(int, dur.split(':'))
                end_ts = int((st + timedelta(hours=h, minutes=m)).timestamp())
            except: end_ts = 0
            embed.description = f"🚀 **開始！** 終了まで： <t:{end_ts}:R>\n\n**配点**： {details['points']}\n📈 [順位表]({url}/standings)"
        elif is_end:
            embed.description = f"🏁 **終了！** お疲れ様でした！\n📈 [最終順位表]({url}/standings) | 📝 [自分の提出]({url}/submissions/me)"
        else:
            embed.description = (f"開始： {st.strftime('%Y-%m-%d %H:%M')}\n時間： {dur} 分\n"
                                 f"Writer： {details['writer']} / Tester： {details['tester']}\n"
                                 f"Rated： {rated} / 配点： {details['points']}\n"
                                 f"開始まで： <t:{int(st.timestamp())}:R>")
            embed.set_footer(text=f"開始時刻：{st.strftime('%Y/%m/%d %p %I:%M:%S')}")
        return embed

    async def check_immediate_announcement(self, channel_id):
        now = datetime.now(JST)
        channel = self.get_channel(channel_id)
        if not channel: return
        status_msg = await channel.send("🚀 全コンテストを高速照合中...")
        async with aiohttp.ClientSession() as session:
            recent_details = await self.fetch_recent_announcements(session, channel)
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                table = soup.find('div', id='contest-table-upcoming')
                if not table: return
                log_txt = "📊 **解析ログ**\n"
                for row in table.find_all('tr')[1:]:
                    cols = row.find_all('td')
                    if len(cols) < 4: continue
                    time_tag = row.find('time')
                    st_dt = datetime.strptime(re.sub(r'\(.*?\)', '', time_tag.text).strip(), '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                    diff = int((st_dt - now).total_seconds() / 60)
                    name_tag = cols[1].find('a')
                    c_url = "https://atcoder.jp" + name_tag['href'].split('?')[0].rstrip('/')
                    log_txt += f"・{name_tag.text[:10]}...: {diff}分 "
                    if 0 < diff <= 2880:
                        info = recent_details.get(c_url, {"writer":"不明","tester":"不明","points":"不明"})
                        await self.broadcast_contest(name_tag.text, c_url, st_dt, cols[2].text.strip(), cols[3].text.strip(), "⏰ 近日開催のコンテスト", info)
                        log_txt += "✅\n"
                    else: log_txt += "⏭️\n"
                await status_msg.edit(content=log_txt[:2000])

    @tasks.loop(minutes=1)
    async def auto_contest_scheduler(self):
        now = datetime.now(JST).replace(second=0, microsecond=0)
        async with aiohttp.ClientSession() as session:
            recent_details = await self.fetch_recent_announcements(session)
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200:
                    print(f"[{now}] ❌ AtCoderへのアクセスに失敗しました: {resp.status}")
                    return
                
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # ターゲットとするテーブルID
                for table_id in ['contest-table-upcoming', 'contest-table-active']:
                    container = soup.find('div', id=table_id)
                    if not container:
                        continue
                    
                    # 確実にtable要素を取得
                    table = container.find('table')
                    if not table:
                        continue

                    rows = table.find_all('tr')[1:] # ヘッダーを除外
                    for row in rows:
                        cols = row.find_all('td')
                        if len(cols) < 4: continue
                        
                        try:
                            # 時刻の抽出と整形
                            time_tag = row.find('time')
                            if not time_tag: continue
                            
                            # 空白文字(nbsp等)を標準スペースに置換し、余計な(Sat)などを除去
                            raw_time = time_tag.text.replace('\xa0', ' ').strip()
                            clean_time = re.sub(r'\(.*?\)', '', raw_time).strip()
                            
                            st_dt = datetime.strptime(clean_time, '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                            
                            dur = cols[2].text.strip()
                            h, m = map(int, dur.split(':'))
                            en_dt = st_dt + timedelta(hours=h, minutes=m)
                            
                            # 1分単位で丸めて比較
                            diff_st = round((st_dt - now).total_seconds() / 60)
                            diff_en = round((en_dt - now).total_seconds() / 60)
                            
                            name_tag = cols[1].find('a')
                            c_url = "https://atcoder.jp" + name_tag['href'].split('?')[0].rstrip('/')
                            rated = cols[3].text.strip()
                            details = recent_details.get(c_url, {"writer":"不明","tester":"不明","points":"不明"})
                            
                            # 通知判定
                            if diff_st == 1440:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, rated, "⏰ 24時間前", details)
                            elif diff_st == 30:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, rated, "⚠️ 30分前", details, is_10min=True)
                            elif diff_st == 0:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, rated, "🚀 開始！", details, is_start=True)
                            elif diff_en == 0:
                                await self.broadcast_contest(name_tag.text, c_url, st_dt, dur, rated, "🏁 終了！", details, is_end=True)

                        except Exception as e:
                            # 原因究明のためのログ出力
                            print(f"⚠️ 解析エラー (Contest: {name_tag.text if 'name_tag' in locals() else 'Unknown'}): {e}")
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
    await interaction.followup.send(f"✅ 告知先を {channel.mention} に設定。")
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
        if type == "c24": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details)
        elif type == "c30": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details, is_10min=True)
        elif type == "cstart": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details, is_start=True)
        elif type == "cend": e = bot.create_contest_embed("Preview", dummy_url, dummy_st, "01:40", "All", dummy_details, is_end=True)
        await interaction.channel.send(content="**Preview**", embed=e)
    await interaction.followup.send("✅ 送信。")

if __name__ == "__main__":
    keep_alive(); bot.run(os.getenv("DISCORD_TOKEN"))
