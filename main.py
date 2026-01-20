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

# あなたが作成したカスタム絵文字ID
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
    if val < 1200: return 0x008000 # 緑
    if val < 2000: return 0x0000FF # 青
    if val < 2800: return 0xFF8000 # 橙
    return 0xFF0000 # 赤

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

    # --- データベース同期 ---
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
            print("✅ Sheets同期完了")
        except Exception as e: print(f"❌ 書き込み失敗: {e}")

    def load_from_sheets(self):
        try:
            ws_user = self.sheet.worksheet("users")
            for r in ws_user.get_all_records():
                key = f"{r['GuildID']}_{r['AtCoderID']}"
                self.user_data[key] = {"guild_id": int(r['GuildID']), "atcoder_id": r['AtCoderID'], "discord_user_id": int(r['DiscordID']), "channel_id": int(r['ChannelID']), "only_ac": str(r['OnlyAC']).lower() == 'true', "last_sub_id": int(r.get('LastSubID', 0))}
            ws_config = self.sheet.worksheet("config")
            for r in ws_config.get_all_records(): self.news_config[str(r['GuildID'])] = int(r['ChannelID'])
            print("✅ 復元完了")
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

    # --- 提出監視 ---
    @tasks.loop(minutes=3)
    async def check_submissions(self):
        async with aiohttp.ClientSession() as session:
            for key, info in list(self.user_data.items()):
                await self.process_submissions(session, info, lookback_seconds=600)

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
        discord_id = info['discord_user_id']
        prob_title = self.problems_map.get(prob_id, prob_id)
        difficulty = self.diff_map.get(prob_id, {}).get('difficulty')
        
        # 1. ユーザー情報取得
        user = self.get_user(discord_id)
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

        # Embed作成
        embed = discord.Embed(color=get_color(difficulty))
        
        # ヘッダー：アイコンとDiscordユーザー名のみ
        embed.set_author(
            name=f"{user_name}",
            icon_url=user_icon
        )

        # 本文：問題名(リンク)、user & result、詳細スペック
        desc = (
            f"**[{prob_title}](https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id})**\n"
            f"user : [{atcoder_id}](https://atcoder.jp/users/{atcoder_id}) / result : {emoji} **[{res}]**\n"
            f"difficulty : {difficulty if difficulty is not None else '---'} / {sub.get('execution_time', '---')}ms / score : {int(sub['point'])}\n"
            f"language : {sub['language']}\n"
            f"📄 [{atcoder_id}さんの提出を見る](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})"
        )
        
        embed.description = desc
        
        # フッター：時刻
        dt = datetime.fromtimestamp(sub['epoch_second'], JST)
        embed.set_footer(text=f"提出時間 : {dt.strftime('%Y年%m月%d日(%a) %H:%M:%S')}")
        
        await channel.send(embed=embed)

        # Embed作成
        embed = discord.Embed(color=get_color(difficulty))
        
        # 2. ヘッダー（アイコン、名前）
        embed.set_author(
            name=f"{user_name} ・ {atcoder_id}",
            icon_url=user_icon
        )

        # 3. 本文（問題名・判定・詳細）
        # 太字や改行位置をご要望通りに設定
        desc = (
            f"**{prob_title}**\n\n"
            f"result : {emoji} **[{res}]**\n"
            f"difficulty : {difficulty if difficulty is not None else '---'} / {sub.get('execution_time', '---')}ms / score : {int(sub['point'])}\n"
            f"language : {sub['language']}\n\n"
            f"📄 [{atcoder_id}さんの提出を見る](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})"
        )
        
        embed.description = desc
        
        # 4. フッター（時刻）
        dt = datetime.fromtimestamp(sub['epoch_second'], JST)
        embed.set_footer(text=f"提出時間 : {dt.strftime('%Y年%m月%d日(%a) %H:%M:%S')}")
        
        await channel.send(embed=embed)
    
    # --- 告知スクレイピング ---
    async def fetch_recent_announcements(self, session):
        results = {}
        try:
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                for box in soup.select('div.col-md-9 div'):
                    h4 = box.find('h4')
                    if h4 and h4.find('a') and '/contests/' in h4.find('a')['href']:
                        c_url = "https://atcoder.jp" + h4.find('a')['href']
                        text = box.get_text()
                        writer, tester, points = "不明", "不明", "不明"
                        w_match = re.search(r"Writer[:：]\s*(.*)", text) or re.search(r"作問[:：]\s*(.*)", text)
                        if w_match: writer = w_match.group(1).split('\n')[0].strip()
                        t_match = re.search(r"Tester[:：]\s*(.*)", text)
                        if t_match: tester = t_match.group(1).split('\n')[0].strip()
                        p_match = re.search(r"(?:配点|Score)[:：]?\s*([0-9\-\s/]+)|配点は\s*([0-9\-\s/]+)\s*です", text)
                        if p_match: points = (p_match.group(1) or p_match.group(2)).strip()
                        results[c_url] = {"writer": writer, "tester": tester, "points": points}
        except: pass
        return results

    # --- コンテストスケジュール (IndexError対策済) ---
    @tasks.loop(minutes=1)
    async def auto_contest_scheduler(self):
        now = datetime.now(JST)
        async with aiohttp.ClientSession() as session:
            recent_details = await self.fetch_recent_announcements(session)
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200: return
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                table = soup.find('div', id='contest-table-upcoming')
                if not table: return
                for row in table.find_all('tr')[1:]:
                    cols = row.find_all('td')
                    if len(cols) < 4: continue 
                    try:
                        st_dt = datetime.strptime(cols[0].text.strip(), '%Y-%m-%d %H:%M:%S%z')
                        name_tag = cols[1].find('a')
                        if not name_tag: continue
                        c_name, c_url = name_tag.text, "https://atcoder.jp" + name_tag['href']
                        duration, rated = cols[2].text.strip(), cols[3].text.strip()
                        details = recent_details.get(c_url, {"writer": "不明", "tester": "不明", "points": "不明"})

                        if timedelta(hours=23, minutes=59) < (st_dt - now) <= timedelta(hours=24):
                            await self.broadcast_contest(c_name, c_url, st_dt, duration, rated, "⏰ 24時間前告知", details)
                        if timedelta(minutes=29) < (st_dt - now) <= timedelta(minutes=30):
                            await self.broadcast_contest(c_name, c_url, st_dt, duration, rated, "⚠️ コンテスト30分前", details, is_30min=True)
                        if timedelta(seconds=0) <= (now - st_dt) < timedelta(minutes=1):
                            await self.broadcast_contest(c_name, c_url, st_dt, duration, rated, "🚀 コンテスト開始！", details, is_start=True)
                        if ":" in duration:
                            h, m = map(int, duration.split(':'))
                            if timedelta(seconds=0) <= (now - (st_dt + timedelta(hours=h, minutes=m))) < timedelta(minutes=1):
                                await self.broadcast_contest(c_name, c_url, st_dt, duration, rated, "🏁 コンテスト終了！", details, is_end=True)
                    except: continue

    async def broadcast_contest(self, name, url, st, dur, rated, label, details, is_30min=False, is_start=False, is_end=False):
        if f"{label}_{url}" in self.sent_notifications: return
        self.sent_notifications.add(f"{label}_{url}")
        embed = discord.Embed(title=name, url=url, color=get_rated_color(rated))
        if is_30min:
            embed.description = f"コンテストまで残り30分となりました\n\nコンテスト名：[{name}]({url})\n👉 [参加登録する]({url})\nレーティング変化： {rated}\n配点： {details['points']}"
        elif is_start:
            embed.description = f"🚀 **開始時刻となりました！**\n終了まで： <t:{int((st + timedelta(minutes=int(dur.split(':')[0])*60 + int(dur.split(':')[1]))).timestamp())}:R>\n\n**【配点内訳】**\n{details['points']}\n\n📈 [順位表]({url}/standings) | 📝 [自分の提出]({url}/submissions/me)"
        elif is_end: embed.description = "🏁 終了時刻となりました。お疲れ様でした！"
        else:
            embed.description = f"コンテストページ： {url}\n開始時刻： {st.strftime('%Y-%m-%d %H:%M')}\nコンテスト時間： {dur} 分\nWriter： {details['writer']}\nTester： {details['tester']}\nレーティング変化： {rated}\n配点： {details['points']}\nコンテスト開始まで： <t:{int(st.timestamp())}:R>"
            embed.set_footer(text=f"コンテスト時間：{st.strftime('%Y年%m月%d日 %p %I:%M:%S').replace('AM','午前').replace('PM','午後')}")
        for cid in self.news_config.values():
            channel = self.get_channel(cid)
            if channel: await channel.send(content=f"**{label}**", embed=embed)

bot = AtCoderBot()

# --- コマンド一覧 ---
@bot.tree.command(name="register", description="提出通知の登録")
async def register(interaction: discord.Interaction, discord_user: discord.Member, atcoder_id: str, channel: discord.TextChannel, only_ac: bool):
    await interaction.response.defer()
    info = {"guild_id": interaction.guild_id, "discord_user_id": discord_user.id, "atcoder_id": atcoder_id, "channel_id": channel.id, "only_ac": only_ac, "last_sub_id": 0}
    bot.user_data[f"{interaction.guild_id}_{atcoder_id}"] = info
    bot.save_to_sheets(); await interaction.followup.send(f"✅ `{atcoder_id}` 登録完了。")
    async with aiohttp.ClientSession() as session: await bot.process_submissions(session, info, lookback_seconds=86400)

@bot.tree.command(name="delete", description="提出通知の削除")
async def delete(interaction: discord.Interaction, atcoder_id: str):
    await interaction.response.defer()
    key = f"{interaction.guild_id}_{atcoder_id}"
    if key in bot.user_data:
        del bot.user_data[key]; bot.save_to_sheets()
        await interaction.followup.send(f"🗑️ `{atcoder_id}` の通知設定を削除しました。")
    else: await interaction.followup.send("登録が見つかりませんでした。", ephemeral=True)

@bot.tree.command(name="notice_set", description="コンテスト告知チャンネルの設定")
async def notice_set(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer()
    bot.news_config[str(interaction.guild_id)] = channel.id
    bot.save_to_sheets(); await interaction.followup.send(f"✅ 告知先を {channel.mention} に設定しました。")

@bot.tree.command(name="notice_delete", description="コンテスト告知設定の削除")
async def notice_delete(interaction: discord.Interaction):
    await interaction.response.defer()
    gid = str(interaction.guild_id)
    if gid in bot.news_config:
        del bot.news_config[gid]; bot.save_to_sheets()
        await interaction.followup.send("🗑️ コンテスト告知の設定を削除しました。")
    else: await interaction.followup.send("設定が見つかりませんでした。", ephemeral=True)

if __name__ == "__main__":
    keep_alive(); bot.run(os.getenv("DISCORD_TOKEN"))
