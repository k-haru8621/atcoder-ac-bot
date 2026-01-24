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

        # --- ここでタイトル（題名）とリンクを設定 ---
        embed = discord.Embed(
            title=prob_title,
            url=f"https://atcoder.jp/contests/{sub['contest_id']}/tasks/{prob_id}",
            color=get_color(difficulty)
        )
        
        # ヘッダー：アイコンとDiscordユーザー名
        embed.set_author(
            name=f"{user_name}",
            icon_url=user_icon
        )

        # 本文：user & result、詳細スペック
        # execution_time が None または存在しない場合に 0 を使うように変更
        exec_time = sub.get('execution_time') if sub.get('execution_time') is not None else 0
        desc = (
            f"user : [{atcoder_id}](https://atcoder.jp/users/{atcoder_id}) / result : {emoji} **[{res}]**\n"
            f"difficulty : {difficulty if difficulty is not None else '---'} / {exec_time}ms / score : {int(sub['point'])}\n"
            f"language : {sub['language']}\n\n"
            f"📄 [{atcoder_id}さんの提出を見る](https://atcoder.jp/contests/{sub['contest_id']}/submissions/{sub['id']})\n"
            f"🔍 [このコンテストの解説を読む](https://atcoder.jp/contests/{sub['contest_id']}/editorial)"
        )
        
        
        embed.description = desc
        
        # フッター：時刻
        dt = datetime.fromtimestamp(sub['epoch_second'], JST)
        embed.set_footer(text=f"提出時刻 : {dt.strftime('%b %d, %Y (%a) %H:%M:%S')}")
        
        await channel.send(embed=embed)
    # --- 告知スクレイピング ---
    async def fetch_recent_announcements(self, session):
        results = {}
        now = datetime.now(JST)
        try:
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200: return {}
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                
                # 「お知らせ」セクションの各投稿をループ
                for post in soup.select('div.panel-body.blog-post'):
                    # 投稿時刻の取得と判定 (24時間以内か)
                    header = post.find_previous('div', class_='panel-heading')
                    time_tag = header.find('time') if header else None
                    if time_tag and 'datetime' in time_tag.attrs:
                        # AtCoderのdatetime属性は "YYYY/MM/DD HH:MM:SS" 形式
                        post_time = datetime.strptime(time_tag['datetime'], '%Y/%m/%d %H:%M:%S').replace(tzinfo=JST)
                        if now - post_time > timedelta(hours=24):
                            continue # 24時間より古い告知はスルー

                    content = post.get_text(separator="\n")
                    link = post.find('a', href=re.compile(r'/contests/[^/]+$'))
                    if not link: continue
                    c_url = "https://atcoder.jp" + link['href'].split('?')[0]
                    
                    # 必要な情報(Writer/配点)を正規表現で引っこ抜く
                    details = {"writer": "不明", "tester": "不明", "points": "不明"}
                    w_m = re.search(r"Writer[:：]\s*(.*)", content) or re.search(r"作問[:：]\s*(.*)", content)
                    if w_m: details["writer"] = w_m.group(1).split('\n')[0].strip()
                    p_m = re.search(r"(?:配点|Score)[:：]?\s*([0-9\-\s/]+)|配点は\s*([0-9\-\s/]+)\s*です", content)
                    if p_m: details["points"] = (p_m.group(1) or p_m.group(2)).strip()
                    
                    results[c_url] = details
        except: pass
        return results

    # --- コンテストスケジュール (IndexError対策済) ---
    @tasks.loop(minutes=1)
    async def auto_contest_scheduler(self):
        now = datetime.now(JST).replace(second=0, microsecond=0)
        async with aiohttp.ClientSession() as session:
            # 24時間以内の最新告知データを取得
            recent_details = await self.fetch_recent_announcements(session)
            
            async with session.get("https://atcoder.jp/home?lang=ja") as resp:
                if resp.status != 200: return
                soup = BeautifulSoup(await resp.text(), 'html.parser')
                table = soup.find('div', id='contest-table-upcoming')
                if not table or not table.find_all('tr'): return
                
                for row in table.find_all('tr')[1:]:
                    cols = row.find_all('td')
                    if len(cols) < 4: continue
                    try:
                        # 開始時刻をパース
                        time_tag = row.find('time')
                        st_str = re.sub(r'\([A-Za-z]+\)', '', time_tag.text).strip()
                        st_dt = datetime.strptime(st_str, '%Y-%m-%d %H:%M:%S%z').astimezone(JST)
                        st_dt_min = st_dt.replace(second=0, microsecond=0)
                        
                        name_tag = cols[1].find('a')
                        c_url = "https://atcoder.jp" + name_tag['href'].split('?')[0]
                        
                        # 残り時間を計算
                        diff_min = int((st_dt_min - now).total_seconds() / 60)

                        # --- 通知判定 ---
                        if diff_min == 1440: # ちょうど24時間前
                            details = recent_details.get(c_url, {"writer":"不明","tester":"不明","points":"不明"})
                            await self.broadcast_contest(name_tag.text, c_url, st_dt, cols[2].text.strip(), cols[3].text.strip(), "⏰ 24時間前告知", details)
                        
                        elif diff_min == 30: # 30分前
                            details = recent_details.get(c_url, {"writer":"不明","tester":"不明","points":"不明"})
                            await self.broadcast_contest(name_tag.text, c_url, st_dt, cols[2].text.strip(), cols[3].text.strip(), "⚠️ コンテスト30分前", details, is_10min=True)
                        
                        elif diff_min == 0: # 開始
                            details = recent_details.get(c_url, {"writer":"不明","tester":"不明","points":"不明"})
                            await self.broadcast_contest(name_tag.text, c_url, st_dt, cols[2].text.strip(), cols[3].text.strip(), "🚀 コンテスト開始！", details, is_start=True)
                    except: continue

    async def broadcast_contest(self, name, url, st, dur, rated, label, details, is_10min=False, is_start=False, is_end=False):
        if f"{label}_{url}" in self.sent_notifications: return
        self.sent_notifications.add(f"{label}_{url}")
        embed = discord.Embed(title=name, url=url, color=get_rated_color(rated))
        if is_10min:
            embed.description = f"コンテストまで残り10分となりました\n\nコンテスト名：[{name}]({url})\n👉 [参加登録する]({url})\nレーティング変化： {rated}\n配点： {details['points']}"
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

# --- プレビューコマンド ---
@bot.tree.command(name="preview", description="各種通知の見た目を確認します")
@app_commands.choices(type=[
    app_commands.Choice(name="提出通知 (AC)", value="ac"),
    app_commands.Choice(name="コンテスト告知 (24時間前)", value="c24"),
    app_commands.Choice(name="コンテスト告知 (10分前)", value="c30"),
    app_commands.Choice(name="コンテスト告知 (開始)", value="cstart"),
    app_commands.Choice(name="コンテスト告知 (終了)", value="cend")
])
async def preview(interaction: discord.Interaction, type: str):
    await interaction.response.defer(ephemeral=True)
    
    # 共通のダミーデータ
    dummy_details = {
        "writer": "AtCoder_Staff",
        "tester": "Admin_Tester",
        "points": "100-200-300-400-500-600"
    }
    dummy_url = "https://atcoder.jp/contests/practice"
    dummy_st = datetime.now(JST)
    
    # 送信先チャンネル（コマンドを打ったチャンネル）
    channel_id = interaction.channel_id

    if type == "ac":
        # AC通知のダミーデータ
        dummy_sub = {
            'id': 0, 'problem_id': 'abc999_a', 'contest_id': 'abc999',
            'user_id': 'atcoder', 'language': 'Python (3.12.1)',
            'point': 100.0, 'execution_time': 15, 'result': 'AC',
            'epoch_second': int(datetime.now().timestamp())
        }
        dummy_info = {
            'atcoder_id': 'atcoder',
            'discord_user_id': interaction.user.id,
            'channel_id': channel_id
        }
        # 既存の関数を流用
        await bot.send_ac_notification(dummy_info, dummy_sub)
        await interaction.followup.send("✅ 提出通知のプレビューを送信しました。")

    else:
        # コンテスト通知系のプレビュー
        # 既存の broadcast_contest を一時的にオーバーライド気味に呼び出す
        # 本来は全サーバーに飛びますが、プレビュー用にこのチャンネルだけに送るよう細工します
        
        # プレビュー用の特殊関数（現在のチャンネルにのみ送る）
        async def send_preview_contest(label, is_10min=False, is_start=False, is_end=False):
            # 元の関数のロジックをコピーしつつ送信先を固定
            embed = discord.Embed(title="AtCoder Beginner Contest 999", url=dummy_url, color=0xFF0000)
            if is_10min:
                embed.description = f"コンテストまで残り10分となりました\n\nコンテスト名：[ABC999]({dummy_url})\n👉 [参加登録する]({dummy_url})\nレーティング変化： All\n配点： {dummy_details['points']}"
            elif is_start:
                embed.description = f"🚀 **開始時刻となりました！**\n終了まで： <t:{int((dummy_st + timedelta(minutes=100)).timestamp())}:R>\n\n**【配点内訳】**\n{dummy_details['points']}\n\n📈 [順位表]({dummy_url}/standings) | 📝 [自分の提出]({dummy_url}/submissions/me)"
            elif is_end:
                embed.description = "🏁 終了時刻となりました。お疲れ様でした！"
            else:
                embed.description = f"コンテストページ： {dummy_url}\n開始時刻： {dummy_st.strftime('%Y-%m-%d %H:%M')}\nコンテスト時間： 100 分\nWriter： {dummy_details['writer']}\nTester： {dummy_details['tester']}\nレーティング変化： All\n配点： {dummy_details['points']}\nコンテスト開始まで： <t:{int(dummy_st.timestamp())}:R>"
            
            await interaction.channel.send(content=f"**{label} (Preview)**", embed=embed)

        if type == "c24": await send_preview_contest("⏰ 24時間前告知")
        elif type == "c30": await send_preview_contest("⚠️ コンテスト10分前", is_10min=True)
        elif type == "cstart": await send_preview_contest("🚀 コンテスト開始！", is_start=True)
        elif type == "cend": await send_preview_contest("🏁 コンテスト終了！", is_end=True)
        
        await interaction.followup.send("✅ コンテスト告知のプレビューを送信しました。")

if __name__ == "__main__":
    keep_alive(); bot.run(os.getenv("DISCORD_TOKEN"))
