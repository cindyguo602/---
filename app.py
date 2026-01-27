import streamlit as st
import pandas as pd
from datetime import datetime, date
import math
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- 設定檔 ---
# Google Sheet 的名稱 (必須跟你雲端硬碟裡的檔名一模一樣)
SHEET_NAME = 'work_log' 

BUDGET_LIMIT = 120000
BASE_RATE = 500
ADMIN_PASSWORD = "1234"

# --- 連接 Google Sheets 的函式 ---
def get_google_sheet_client():
    # 從 Streamlit Cloud 的 Secrets 裡讀取憑證
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    # 這裡會讀取我們等一下在網頁上設定的 secrets
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client

def load_data():
    try:
        client = get_google_sheet_client()
        sheet = client.open(SHEET_NAME).sheet1
        data = sheet.get_all_records()
        df = pd.DataFrame(data)
        
        # 如果是空的，建立欄位
        if df.empty:
            return pd.DataFrame(columns=['Name', 'Scheme', 'Action', 'Time', 'Timestamp'])
            
        # 轉換時間格式
        if 'Time' in df.columns:
            df['Time'] = pd.to_datetime(df['Time'])
        return df
    except Exception as e:
        # 如果找不到檔案或連線失敗
        st.error(f"無法讀取 Google Sheet: {e}")
        return pd.DataFrame(columns=['Name', 'Scheme', 'Action', 'Time', 'Timestamp'])

def save_data(df):
    try:
        client = get_google_sheet_client()
        sheet = client.open(SHEET_NAME).sheet1
        
        # 因為 gspread 寫入需要字串，先把時間轉回字串
        save_df = df.copy()
        save_df['Time'] = save_df['Time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # 全量更新 (簡單暴力，適合小團隊資料量)
        sheet.clear()
        # 把欄位名稱寫回去
        sheet.append_row(save_df.columns.tolist())
        # 把資料寫回去
        sheet.append_rows(save_df.values.tolist())
        
    except Exception as e:
        st.error(f"存檔失敗: {e}")

# --- 以下邏輯與原本相同，僅省略部分重複註解 ---

def recalculate_timestamp(df):
    try:
        df['Time'] = pd.to_datetime(df['Time'])
        df['Timestamp'] = df['Time'].apply(lambda x: x.timestamp())
        return df, True
    except:
        return df, False

def get_user_state(df, name):
    if df.empty: return False, None, None
    current_time = datetime.now().timestamp()
    user_records = df[(df['Name'] == name) & (df['Timestamp'] <= current_time + 5)].sort_values('Timestamp')
    if user_records.empty: return False, None, None
    last_record = user_records.iloc[-1]
    if last_record['Action'] == '上班':
        return True, last_record['Scheme'], last_record['Time']
    return False, None, None

def check_cooldown(df, name, cooldown_seconds=10):
    if df.empty: return True, 0
    user_records = df[df['Name'] == name].copy()
    if user_records.empty: return True, 0
    current_time = datetime.now().timestamp()
    valid_records = user_records[user_records['Timestamp'] <= (current_time + 5)]
    if valid_records.empty: return True, 0
    last_record_time = valid_records['Timestamp'].max()
    diff = current_time - last_record_time
    if 0 <= diff < cooldown_seconds:
        return False, int(cooldown_seconds - diff)
    return True, 0

def calculate_salary_stats(df):
    if df.empty: return pd.DataFrame(), pd.DataFrame()
    records = []
    for (name, scheme), group in df.groupby(['Name', 'Scheme']):
        group = group.sort_values('Timestamp')
        start_time = None
        for _, row in group.iterrows():
            if row['Action'] == '上班':
                start_time = row['Timestamp']
            elif row['Action'] == '下班' and start_time is not None:
                end_time = row['Timestamp']
                duration_seconds = end_time - start_time
                minutes = math.ceil(duration_seconds / 60)
                hours = minutes / 60.0
                records.append({
                    'Name': name, 'Scheme': scheme, 'Date': pd.to_datetime(row['Time']).date(),
                    'Time_In': pd.to_datetime(start_time, unit='s'),
                    'Time_Out': pd.to_datetime(end_time, unit='s'),
                    'Minutes': minutes, 'Hours': hours, 'Status': 'Done'
                })
                start_time = None 
        if start_time is not None:
            records.append({
                'Name': name, 'Scheme': scheme, 'Date': pd.to_datetime(start_time, unit='s').date(),
                'Time_In': pd.to_datetime(start_time, unit='s'), 'Time_Out': pd.NaT,
                'Minutes': 0, 'Hours': 0.0, 'Status': 'Working'
            })
    if not records: return pd.DataFrame(), pd.DataFrame()
    records_df = pd.DataFrame(records)
    scheme_stats = []
    rate_map = {}
    for scheme in ['方案1', '方案2', '方案3']:
        scheme_data = records_df[(records_df['Scheme'] == scheme) & (records_df['Status'] == 'Done')]
        total_hours = scheme_data['Hours'].sum()
        if total_hours * BASE_RATE > BUDGET_LIMIT:
            current_rate = BUDGET_LIMIT / total_hours if total_hours > 0 else BASE_RATE
            status = "⚠️ 已達上限"
            is_over = True
        else:
            current_rate = BASE_RATE
            status = "✅ 預算內"
            is_over = False
        rate_map[scheme] = current_rate
        scheme_stats.append({'Scheme': scheme, 'Total_Hours': total_hours, 'Current_Rate': current_rate, 'Total_Spent': total_hours * current_rate, 'Status': status})
    records_df['Rate_Applied'] = records_df['Scheme'].map(rate_map)
    records_df['Earnings'] = records_df.apply(lambda x: x['Hours'] * x['Rate_Applied'] if x['Status'] == 'Done' else 0, axis=1)
    return records_df, pd.DataFrame(scheme_stats)

def get_greeting():
    h = datetime.now().hour
    return "早安 ☀️" if 5<=h<12 else "午安 ☕" if 12<=h<18 else "晚安 🌙"

# --- 主程式 ---
st.set_page_config(page_title="威尼斯返台展打卡", layout="wide")
st.title("🏗️ 威尼斯返台展-開發商組 模型製作")

if 'show_balloons' in st.session_state and st.session_state['show_balloons']:
    st.balloons()
    st.toast('打卡成功！', icon='✅')
    st.session_state['show_balloons'] = False

df = load_data() # 改成讀取 Google Sheet

# --- Sidebar ---
st.sidebar.header("📍 打卡區")
names = sorted(df['Name'].unique().tolist()) if not df.empty else []
name_opt = ["-- 請選擇 --"] + names + ["➕ 新增成員..."]
u_name = st.sidebar.selectbox("我是誰？", name_opt)
final_name = st.sidebar.text_input("輸入新名字") if u_name == "➕ 新增成員..." else u_name if u_name != "-- 請選擇 --" else ""

if final_name:
    is_work, cur_sch, st_time = get_user_state(df, final_name)
    st.sidebar.markdown(f"### {get_greeting()}，{final_name}！")
    now = datetime.now()
    
    if is_work:
        st.sidebar.success(f"🟢 工作中：**{cur_sch}**")
        st.sidebar.caption(f"開始：{st_time.strftime('%H:%M')}")
        if st.sidebar.button("⏹️ 下班打卡", use_container_width=True, type="primary"):
            ok, wait = check_cooldown(df, final_name)
            if not ok: st.sidebar.error(f"太快了，等 {wait} 秒")
            else:
                new_row = pd.DataFrame([{'Name': final_name, 'Scheme': cur_sch, 'Action': '下班', 'Time': now, 'Timestamp': now.timestamp()}])
                save_data(pd.concat([df, new_row], ignore_index=True))
                st.session_state['show_balloons'] = True
                time.sleep(1)
                st.rerun()
    else:
        st.sidebar.warning("⚪ 休息中")
        sch_opt = st.sidebar.selectbox("方案", ["方案1", "方案2", "方案3"])
        if st.sidebar.button("▶️ 上班打卡", use_container_width=True):
            ok, wait = check_cooldown(df, final_name)
            if not ok: st.sidebar.error(f"太快了，等 {wait} 秒")
            else:
                new_row = pd.DataFrame([{'Name': final_name, 'Scheme': sch_opt, 'Action': '上班', 'Time': now, 'Timestamp': now.timestamp()}])
                save_data(pd.concat([df, new_row], ignore_index=True))
                st.session_state['show_balloons'] = True
                time.sleep(1)
                st.rerun()

st.sidebar.divider()
st.sidebar.info(f"💰 時薪: ${BASE_RATE}\n📉 預算: ${BUDGET_LIMIT/10000}萬")

# --- Tabs ---
records_df, scheme_stats_df = calculate_salary_stats(df)
t1, t2, t3 = st.tabs(["💰 個人報表", "📊 專案監控", "🔧 後台管理"])

with t1: # 個人
    if final_name and not records_df.empty:
        my_recs = records_df[records_df['Name']==final_name].copy()
        if not my_recs.empty:
            c1,c2,c3 = st.columns(3)
            c1.metric("累計薪資", f"${my_recs['Earnings'].sum():,.0f}")
            c2.metric("結算工時", f"{my_recs[my_recs['Status']=='Done']['Hours'].sum():.2f} hr")
            c3.success("🟢 工作中") if is_work else c3.info("⚪ 已下班")
            st.write("---")
            for d in sorted(my_recs['Date'].unique(), reverse=True):
                st.markdown(f"#### 📅 {d}")
                day_recs = my_recs[my_recs['Date']==d]
                for sch in sorted(day_recs['Scheme'].unique()):
                    st.markdown(f"**🔹 {sch}**")
                    disp = []
                    for _,r in day_recs[day_recs['Scheme']==sch].iterrows():
                        disp.append({
                            "上班": r['Time_In'].strftime("%H:%M"),
                            "下班": r['Time_Out'].strftime("%H:%M") if pd.notna(r['Time_Out']) else "⏳ ...",
                            "工時": f"{r['Hours']:.2f}" if pd.notna(r['Time_Out']) else "-",
                            "薪資": f"${r['Earnings']:.0f}" if pd.notna(r['Time_Out']) else "-"
                        })
                    st.dataframe(pd.DataFrame(disp), use_container_width=True, hide_index=True)
        else: st.info("無紀錄")
    else: st.info("請選擇名字")

with t2: # 監控
    if not scheme_stats_df.empty:
        sel = st.radio("篩選", ["全部", "方案1", "方案2", "方案3"], horizontal=True)
        tgt = scheme_stats_df if sel=="全部" else scheme_stats_df[scheme_stats_df['Scheme']==sel]
        for _,r in tgt.iterrows():
            c1,c2 = st.columns([2,1])
            c1.markdown(f"### {r['Scheme']}")
            c2.markdown(f"時薪: **${r['Current_Rate']:.2f}**")
            st.progress(min(r['Total_Spent']/BUDGET_LIMIT, 1.0), f"消耗: ${r['Total_Spent']:,.0f} / ${BUDGET_LIMIT:,.0f}")
            st.divider()

with t3: # 後台
    pwd = st.text_input("密碼", type="password")
    if pwd == ADMIN_PASSWORD:
        st.success("已解鎖")
        st.markdown("### 🟢 線上人員")
        if not records_df.empty:
            w_df = records_df[records_df['Status']=='Working'].copy()
            if not w_df.empty:
                now_ts = datetime.now().timestamp()
                w_df['時數'] = w_df['Time_In'].apply(lambda x: f"{int((now_ts-x.timestamp())//3600)}時 {int(((now_ts-x.timestamp())%3600)//60)}分")
                w_df['打卡'] = w_df['Time_In'].dt.strftime('%H:%M')
                st.dataframe(w_df[['Name','Scheme','打卡','時數']], use_container_width=True, hide_index=True)
            else: st.info("無人上班")
        
        st.markdown("### 📋 資料編輯")
        # 這裡簡化編輯器，因為 Google Sheet 同步比較慢，建議只做簡單顯示
        # 若要編輯，直接去 Google Sheet 改最快！
        st.info("💡 如需修改歷史資料，請直接打開 Google 試算表進行編輯，這裡僅供檢視。")
        st.link_button("前往 Google 試算表", f"https://docs.google.com/spreadsheets/d/") # 你可以填入網址
        
        st.dataframe(df.sort_values('Time', ascending=False), use_container_width=True)