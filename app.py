import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import math
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- 設定檔 ---
SHEET_NAME = 'work_log' 
BUDGET_LIMIT = 120000  # 預算上限
BASE_RATE = 500        # 基礎時薪
ADMIN_PASSWORD = "1234"

# --- 核心：取得台灣時間 (解決時間不準問題) ---
def get_taiwan_now():
    # 雲端主機通常是 UTC，所以我們要手動 +8 小時
    return datetime.utcnow() + timedelta(hours=8)

# --- 連接 Google Sheets 的函式 ---
def get_google_sheet_client():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client

def load_data():
    try:
        client = get_google_sheet_client()
        sheet = client.open(SHEET_NAME).sheet1
        
        # 改用 get_all_values 以避免標題錯誤導致崩潰
        data = sheet.get_all_values()
        
        # 定義標準欄位
        expected_cols = ['Name', 'Scheme', 'Action', 'Time', 'Timestamp']
        
        # 情況 1: 試算表完全空白
        if not data:
            return pd.DataFrame(columns=expected_cols)
            
        # 取得第一列當作標題
        headers = data[0]
        
        # 情況 2: 標題列不正確 (缺少 Name 或其他必要欄位)
        # 這是解決 KeyError 的關鍵：如果標題不對，就強制回傳空的標準表格，讓程式能跑
        if not set(expected_cols).issubset(set(headers)):
            return pd.DataFrame(columns=expected_cols)
            
        # 情況 3: 正常讀取 (排除第一列標題)
        df = pd.DataFrame(data[1:], columns=headers)
        
        if 'Time' in df.columns:
            df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
        
        # 確保數值欄位是數字 (防呆)
        if 'Timestamp' in df.columns:
            df['Timestamp'] = pd.to_numeric(df['Timestamp'], errors='coerce')
            
        return df
        
    except Exception as e:
        # 萬一連線失敗，回傳空表格防止網頁掛掉
        st.error(f"無法讀取 Google Sheet: {e}")
        return pd.DataFrame(columns=['Name', 'Scheme', 'Action', 'Time', 'Timestamp'])

def save_data(df):
    try:
        client = get_google_sheet_client()
        sheet = client.open(SHEET_NAME).sheet1
        
        save_df = df.copy()
        # 存檔時，確保時間轉為字串
        save_df['Time'] = save_df['Time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        
        sheet.clear()
        sheet.append_row(save_df.columns.tolist())
        sheet.append_rows(save_df.values.tolist())
        
    except Exception as e:
        st.error(f"存檔失敗: {e}")

def recalculate_timestamp(df):
    try:
        # 確保格式為 datetime
        df['Time'] = pd.to_datetime(df['Time'])
        # 重新計算 Timestamp (用來排序和計算工時)
        df['Timestamp'] = df['Time'].apply(lambda x: x.timestamp())
        return df, True
    except:
        return df, False

def get_user_state(df, name):
    if df.empty: return False, None, None
    
    # 改用台灣時間
    current_time = get_taiwan_now().timestamp()
    
    # 稍微放寬緩衝，避免邊界時間問題
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
    
    # 改用台灣時間
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
    # 確保資料依照時間排序
    df = df.sort_values('Timestamp')
    
    for (name, scheme), group in df.groupby(['Name', 'Scheme']):
        start_time = None
        for _, row in group.iterrows():
            if row['Action'] == '上班':
                start_time = row['Timestamp']
            elif row['Action'] == '下班' and start_time is not None:
                end_time = row['Timestamp']
                duration_seconds = end_time - start_time
                
                # 只有大於 0 的才算有效工時
                if duration_seconds > 0:
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
    
    # --- 關鍵邏輯：計算時薪與預算 ---
    for scheme in ['方案1', '方案2', '方案3']:
        scheme_data = records_df[(records_df['Scheme'] == scheme) & (records_df['Status'] == 'Done')]
        total_hours = scheme_data['Hours'].sum()
        
        # 1. 先用 500 算算看有沒有爆
        potential_cost = total_hours * BASE_RATE
        
        if potential_cost > BUDGET_LIMIT:
            # 2. 如果爆了，時薪 = 120000 / 總時數 (無條件捨去小數點後兩位，或保留精確度皆可，這裡保留運算)
            current_rate = BUDGET_LIMIT / total_hours if total_hours > 0 else BASE_RATE
            status = "⚠️ 已達上限 (自動降薪)"
            is_over = True
        else:
            # 3. 沒爆就是 500
            current_rate = BASE_RATE
            status = "✅ 預算內"
            is_over = False
            
        rate_map[scheme] = current_rate
        scheme_stats.append({
            'Scheme': scheme, 
            'Total_Hours': total_hours, 
            'Current_Rate': current_rate, 
            'Total_Spent': total_hours * current_rate, 
            'Status': status
        })
    # --------------------------------
        
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
    
    # 改用台灣時間
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
            
            if is_work:
                c3.success("🟢 工作中")
            else:
                c3.info("⚪ 已下班")
            
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
            # 這裡會顯示目前的動態時薪
            c2.markdown(f"結算時薪: **${r['Current_Rate']:.2f}**")
            st.progress(min(r['Total_Spent']/BUDGET_LIMIT, 1.0), f"消耗: ${r['Total_Spent']:,.0f} / ${BUDGET_LIMIT:,.0f}")
            
            # --- [這裡就是你要的：人員薪資詳情] ---
            with st.expander(f"📋 點擊展開 {r['Scheme']} 人員薪資表"):
                if not records_df.empty:
                    # 篩選出這個方案且已結算的紀錄
                    scheme_details = records_df[(records_df['Scheme'] == r['Scheme']) & (records_df['Status'] == 'Done')]
                    if not scheme_details.empty:
                        # 依照人名分組加總，這裡的 Earnings 已經是「調整過時薪」的結果
                        person_sum = scheme_details.groupby('Name').agg({'Hours': 'sum', 'Earnings': 'sum'}).reset_index()
                        st.dataframe(person_sum.style.format({"Hours": "{:.2f} hr", "Earnings": "${:,.0f}"}), use_container_width=True)
                    else:
                        st.caption("尚無已結算薪資紀錄")
            # ---------------------------
            st.divider()
    else:
        st.info("尚無資料，無法計算預算。")

with t3:
    pwd = st.text_input("密碼", type="password")
    if pwd == ADMIN_PASSWORD:
        st.success("已解鎖")
        
        # --- 1. 即時監控 ---
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

        # --- 2. Google 同步編輯器 ---
        st.markdown("### 📋 資料編輯 (將同步至 Google Sheet)")
        
        col_filter1, col_filter2 = st.columns(2)
        all_names = sorted(df['Name'].unique().tolist()) if not df.empty else []
        all_schemes = ["方案1", "方案2", "方案3"]
        
        with col_filter1:
            st.markdown("##### 1. 日期範圍")
            c_d1, c_d2 = st.columns(2)
            # 預設顯示今天的資料，方便編輯
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

        # 編輯器
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
                    # 延遲 2 秒確保 Google 存檔完成，這樣重整後預算才會更新
                    time.sleep(2)
                    st.rerun()
                else:
                    st.error("❌ 時間格式錯誤！")
