import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import math
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- 設定檔 ---
SHEET_NAME = 'work_log' 
SUMMARY_SHEET_NAME = 'daily_summary' # 這是新分頁的名稱
BUDGET_LIMIT = 120000
BASE_RATE = 500
ADMIN_PASSWORD = "1234"

# --- 核心：取得台灣時間 ---
def get_taiwan_now():
    return datetime.utcnow() + timedelta(hours=8)

# --- 連接 Google Sheets ---
def get_google_sheet_client():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client

# --- 讀取資料 (防呆版) ---
def load_data():
    try:
        client = get_google_sheet_client()
        sheet = client.open(SHEET_NAME).sheet1
        data = sheet.get_all_values()
        
        expected_cols = ['Name', 'Scheme', 'Action', 'Time', 'Timestamp']
        df = pd.DataFrame()
        
        if not data:
            df = pd.DataFrame(columns=expected_cols)
        else:
            headers = data[0]
            if not set(expected_cols).issubset(set(headers)):
                df = pd.DataFrame(columns=expected_cols)
            else:
                df = pd.DataFrame(data[1:], columns=headers)
        
        if 'Time' in df.columns:
            df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
        
        if 'Timestamp' in df.columns:
            df['Timestamp'] = pd.to_numeric(df['Timestamp'], errors='coerce')
            
        return df
    except Exception as e:
        st.error(f"無法讀取 Google Sheet: {e}")
        empty_df = pd.DataFrame(columns=['Name', 'Scheme', 'Action', 'Time', 'Timestamp'])
        empty_df['Time'] = pd.to_datetime(empty_df['Time'])
        return empty_df

# --- [新增功能] 更新「每日考勤匯總表」 ---
def update_daily_summary_sheet(df):
    """
    這個函式會自動整理資料，產出一張「一人一天一行」的清楚報表
    並寫入 Google Sheet 的第二個分頁
    """
    try:
        # 1. 先算出詳細區段 (為了計算實際工時)
        records = []
        df = df.sort_values('Timestamp')
        for (name, scheme), group in df.groupby(['Name', 'Scheme']):
            start_time = None
            for _, row in group.iterrows():
                if row['Action'] == '上班':
                    start_time = row['Timestamp']
                elif row['Action'] == '下班' and start_time is not None:
                    end_time = row['Timestamp']
                    duration = end_time - start_time
                    if duration > 0:
                        records.append({
                            'Name': name,
                            'Date': pd.to_datetime(row['Time']).date(),
                            'Start': pd.to_datetime(start_time, unit='s'),
                            'End': pd.to_datetime(end_time, unit='s'),
                            'Hours': duration / 3600
                        })
                    start_time = None
        
        if not records:
            return # 沒資料就不處理

        detail_df = pd.DataFrame(records)
        
        # 2. 進行匯總 (Group by Name + Date)
        # 邏輯：取當天最早的時間當上班，最晚的時間當下班，總時數加總
        summary_df = detail_df.groupby(['Name', 'Date']).agg(
            最早上班=('Start', 'min'),
            最晚下班=('End', 'max'),
            實際工時=('Hours', 'sum')
        ).reset_index()

        # 3. 格式化顯示
        summary_df['Date'] = summary_df['Date'].astype(str)
        summary_df['最早上班'] = summary_df['最早上班'].dt.strftime('%H:%M:%S')
        summary_df['最晚下班'] = summary_df['最晚下班'].dt.strftime('%H:%M:%S')
        summary_df['實際工時'] = summary_df['實際工時'].round(2)
        
        # 4. 寫入 Google Sheet 的第二個分頁
        client = get_google_sheet_client()
        spreadsheet = client.open(SHEET_NAME)
        
        try:
            worksheet = spreadsheet.worksheet(SUMMARY_SHEET_NAME)
        except:
            # 如果分頁不存在，就建立一個新的
            worksheet = spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows="1000", cols="5")
        
        worksheet.clear()
        # 寫入中文標題
        headers = ['姓名', '日期', '上班時間', '下班時間', '實際工時(小時)']
        worksheet.append_row(headers)
        worksheet.append_rows(summary_df.values.tolist())
        
    except Exception as e:
        print(f"匯總表更新失敗: {e}") # 不顯示在前端以免干擾使用者，但在後台記錄

# --- 存檔 (同時觸發匯總表更新) ---
def save_data(df):
    try:
        client = get_google_sheet_client()
        sheet = client.open(SHEET_NAME).sheet1
        
        save_df = df.copy()
        save_df['Time'] = save_df['Time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        
        sheet.clear()
        sheet.append_row(save_df.columns.tolist())
        sheet.append_rows(save_df.values.tolist())
        
        # [關鍵] 存檔完後，順便更新匯總表
        update_daily_summary_sheet(df)
        
    except Exception as e:
        st.error(f"存檔失敗: {e}")

# --- 其他輔助函式 ---
def recalculate_timestamp(df):
    try:
        df['Time'] = pd.to_datetime(df['Time'])
        df['Timestamp'] = df['Time'].apply(lambda x: x.timestamp())
        return df, True
    except:
        return df, False

def get_user_state(df, name):
    if df.empty: return False, None, None
    current_time = get_taiwan_now().timestamp()
    user_records = df[(df['Name'] == name) & (df['Timestamp'] <= current_time + 60)].sort_values('Timestamp')
    if user_records.empty: return False, None, None
    last_record = user_records.iloc[-1]
    if last_record['Action'] == '上班':
        return True, last_record['Scheme'], last_record['Time']
    return False, None, None

def check_cooldown(df, name, cooldown_seconds=10):
    if df.empty: return True, 0
    user_records = df[df['Name'] == name].copy()
    if user_records.empty: return True, 0
    current_time = get_taiwan_now().timestamp()
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
    df = df.sort_values('Timestamp')
    for (name, scheme), group in df.groupby(['Name', 'Scheme']):
        start_time = None
        for _, row in group.iterrows():
            if row['Action'] == '上班':
                start_time = row['Timestamp']
            elif row['Action'] == '下班' and start_time is not None:
                end_time = row['Timestamp']
                duration = end_time - start_time
                if duration > 0:
                    records.append({
                        'Name': name, 'Scheme': scheme, 'Date': pd.to_datetime(row['Time']).date(),
                        'Time_In': pd.to_datetime(start_time, unit='s'),
                        'Time_Out': pd.to_datetime(end_time, unit='s'),
                        'Minutes': math.ceil(duration / 60), 'Hours': duration / 3600, 'Status': 'Done'
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
        potential_cost = total_hours * BASE_RATE
        if potential_cost > BUDGET_LIMIT:
            current_rate = BUDGET_LIMIT / total_hours if total_hours > 0 else BASE_RATE
            status = "⚠️ 已達上限 (自動降薪)"
        else:
            current_rate = BASE_RATE
            status = "✅ 預算內"
        rate_map[scheme] = current_rate
        scheme_stats.append({'Scheme': scheme, 'Total_Hours': total_hours, 'Current_Rate': current_rate, 'Total_Spent': total_hours * current_rate, 'Status': status})
    records_df['Rate_Applied'] = records_df['Scheme'].map(rate_map)
    records_df['Earnings'] = records_df.apply(lambda x: x['Hours'] * x['Rate_Applied'] if x['Status'] == 'Done' else 0, axis=1)
    return records_df, pd.DataFrame(scheme_stats)

def get_greeting():
    h = get_taiwan_now().hour
    return "早安 ☀️" if 5<=h<12 else "午安 ☕" if 12<=h<18 else "晚安 🌙"

# --- 主程式 ---
st.set_page_config(page_title="威尼斯返台展打卡", layout="wide")
st.title("🏗️ 威尼斯返台展-開發商組 模型製作")

if 'show_balloons' in st.session_state and st.session_state['show_balloons']:
    st.balloons()
    st.toast('打卡成功！', icon='✅')
    st.session_state['show_balloons'] = False

df = load_data()

# --- Sidebar ---
st.sidebar.header("📍 打卡區")
names = sorted(df['Name'].unique().tolist()) if not df.empty else []
name_opt = ["-- 請選擇 --"] + names + ["➕ 新增成員..."]
u_name = st.sidebar.selectbox("我是誰？", name_opt)
final_name = st.sidebar.text_input("輸入新名字") if u_name == "➕ 新增成員..." else u_name if u_name != "-- 請選擇 --" else ""

if final_name:
    is_work, cur_sch, st_time = get_user_state(df, final_name)
    st.sidebar.markdown(f"### {get_greeting()}，{final_name}！")
    now = get_taiwan_now()
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
st.sidebar.info(f"💰 基礎時薪: ${BASE_RATE}\n📉 預算上限: ${BUDGET_LIMIT/10000}萬")

# --- Tabs ---
records_df, scheme_stats_df = calculate_salary_stats(df)
t1, t2, t3 = st.tabs(["💰 個人報表", "📊 專案監控", "🔧 後台管理"])

with t1:
    if final_name and not records_df.empty:
        my_recs = records_df[records_df['Name']==final_name].copy()
        if not my_recs.empty:
            c1,c2,c3 = st.columns(3)
            c1.metric("累計薪資", f"${my_recs['Earnings'].sum():,.0f}")
            c2.metric("結算工時", f"{my_recs[my_recs['Status']=='Done']['Hours'].sum():.2f} hr")
            if is_work: c3.success("🟢 工作中")
            else: c3.info("⚪ 已下班")
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

with t2:
    if not scheme_stats_df.empty:
        sel = st.radio("篩選", ["全部", "方案1", "方案2", "方案3"], horizontal=True)
        tgt = scheme_stats_df if sel=="全部" else scheme_stats_df[scheme_stats_df['Scheme']==sel]
        for _,r in tgt.iterrows():
            c1,c2 = st.columns([2,1])
            c1.markdown(f"### {r['Scheme']}")
            c2.markdown(f"結算時薪: **${r['Current_Rate']:.2f}**")
            st.progress(min(r['Total_Spent']/BUDGET_LIMIT, 1.0), f"消耗: ${r['Total_Spent']:,.0f} / ${BUDGET_LIMIT:,.0f}")
            with st.expander(f"📋 點擊展開 {r['Scheme']} 人員薪資表"):
                if not records_df.empty:
                    scheme_details = records_df[(records_df['Scheme'] == r['Scheme']) & (records_df['Status'] == 'Done')]
                    if not scheme_details.empty:
                        person_sum = scheme_details.groupby('Name').agg({'Hours': 'sum', 'Earnings': 'sum'}).reset_index()
                        st.dataframe(person_sum.style.format({"Hours": "{:.2f} hr", "Earnings": "${:,.0f}"}), use_container_width=True)
                    else: st.caption("尚無已結算薪資紀錄")
            st.divider()
    else: st.info("尚無資料，無法計算預算。")

with t3:
    pwd = st.text_input("密碼", type="password")
    if pwd == ADMIN_PASSWORD:
        st.success("已解鎖")
        st.markdown("### 🟢 線上人員")
        if not records_df.empty:
            w_df = records_df[records_df['Status']=='Working'].copy()
            if not w_df.empty:
                now_ts = get_taiwan_now().timestamp()
                w_df['時數'] = w_df['Time_In'].apply(lambda x: f"{int((now_ts-x.timestamp())//3600)}時 {int(((now_ts-x.timestamp())%3600)//60)}分")
                w_df['打卡'] = w_df['Time_In'].dt.strftime('%H:%M')
                st.dataframe(w_df[['Name','Scheme','打卡','時數']], use_container_width=True, hide_index=True)
            else: st.info("無人上班")
        st.divider()
        st.markdown("### 📋 資料編輯 (將同步至 Google Sheet)")
        col_filter1, col_filter2 = st.columns(2)
        all_names = sorted(df['Name'].unique().tolist()) if not df.empty else []
        all_schemes = ["方案1", "方案2", "方案3"]
        with col_filter1:
            st.markdown("##### 1. 日期範圍")
            c_d1, c_d2 = st.columns(2)
            taiwan_today = get_taiwan_now().date()
            start_date = c_d1.date_input("開始", date(2024, 1, 1))
            end_date = c_d2.date_input("結束", taiwan_today)
        with col_filter2:
            st.markdown("##### 2. 詳細篩選")
            c_f1, c_f2 = st.columns(2)
            filter_names = c_f1.multiselect("篩選人員", options=all_names, placeholder="留空則顯示全部")
            filter_schemes = c_f2.multiselect("篩選方案", options=all_schemes, placeholder="留空則顯示全部")
        mask = (df['Time'].dt.date >= start_date) & (df['Time'].dt.date <= end_date)
        if filter_names: mask = mask & (df['Name'].isin(filter_names))
        if filter_schemes: mask = mask & (df['Scheme'].isin(filter_schemes))
        filtered_df = df.loc[mask].copy()
        if not filtered_df.empty:
            filtered_df = filtered_df.sort_values(by=['Time', 'Name', 'Scheme'], ascending=[False, True, True])
        edited_df = st.data_editor(
            filtered_df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "Name": st.column_config.SelectboxColumn("姓名", options=all_names + ["新增..."], required=True),
                "Scheme": st.column_config.SelectboxColumn("方案", options=all_schemes, required=True),
                "Action": st.column_config.SelectboxColumn("動作", options=["上班", "下班"], required=True),
                "Time": st.column_config.DatetimeColumn("打卡時間", format="Y-M-D HH:mm:ss", step=60),
                "Timestamp": st.column_config.NumberColumn("系統秒數", disabled=True)
            },
            key="admin_editor"
        )
        if st.button("💾 儲存並同步至 Google Sheet", type="primary"):
            with st.spinner("正在寫入 Google Sheet..."):
                remaining_df = df.loc[~mask]
                new_full_df = pd.concat([remaining_df, edited_df], ignore_index=True)
                new_full_df, success = recalculate_timestamp(new_full_df)
                if success:
                    save_data(new_full_df)
                    st.success("✅ 資料已同步！即將重新載入...")
                    time.sleep(2)
                    st.rerun()
                else: st.error("❌ 時間格式錯誤！")
