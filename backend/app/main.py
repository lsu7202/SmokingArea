# app/main.py
from fastapi import FastAPI, Depends, HTTPException, status
import os
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
import pandas as pd
import pyproj
from contextlib import asynccontextmanager
import asyncio # 비동기 컨텍스트에서 동기 함수 실행을 위해 필요
import math


# --- 설정 변수 ---
DATABASE_URL = "postgresql://Team_ten:1234@db:5432/tabaco_retail"
CSV_PATH = "/app/data/address.csv" # Docker 컨테이너 내부 경로


# --- SQLAlchemy 엔진 및 세션 설정 (FastAPI 비동기 환경에 맞게 조정) ---
# 동기 엔진 생성 (FastAPI에서 직접 사용하지 않고, asyncio.to_thread로 감싸서 사용)
sync_engine = create_engine(DATABASE_URL) 
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


# --- DB 의존성 주입 함수 (실제 DB 연결 사용) ---
async def get_db():
    """
    SQLAlchemy 세션 객체를 제공하고 요청 완료 후 닫습니다.
    비동기 컨텍스트에서 동기 DB 작업을 위해 asyncio.to_thread를 사용합니다.
    """
    db = SessionLocal()
    try:
        # 이 시점에서 DB 연결이 실제로 이루어짐 (session.connection() 등)
        print("Database session acquired.")
        yield db
    finally:
        db.close()
        print("Database session closed.")


# --- address.csv → DB 로딩 함수 ---
async def initialize_address_table():
    """
    애플리케이션 시작 시 address 테이블이 비어있으면 CSV 데이터를 삽입합니다.
    """
    try:
        print("🔍 address 테이블 상태 확인 중...")
        
        # inspect를 사용하여 테이블 존재 여부 확인
        # 동기 작업을 비동기로 실행
        table_exists = await asyncio.to_thread(
            lambda: inspect(sync_engine).has_table("address")
        )

        if not table_exists:
            print("⚙️ address 테이블이 존재하지 않습니다. 생성 후 CSV 데이터를 삽입합니다...")
            # CSV 로드
            df = await asyncio.to_thread(pd.read_csv, CSV_PATH)

            # 비어있는 문자열/null 값을 처리
            df[['landlot_address', 'road_name_address']] = df[['landlot_address', 'road_name_address']].fillna("비어있음")
            
            # x, y 좌표가 비어 있으면 -1로 대체 (int/float 타입 호환을 위해)
            if 'x' in df.columns:
                df['x'] = df['x'].apply(lambda v: v if pd.notna(v) and v != '' else -1.0) # float으로 일관성 유지
            if 'y' in df.columns:
                df['y'] = df['y'].apply(lambda v: v if pd.notna(v) and v != '' else -1.0) # float으로 일관성 유지

            # DataFrame을 SQL 테이블로 삽입 (append 모드)
            # 동기 작업을 비동기로 실행
            await asyncio.to_thread(
                df.to_sql, 'address', con=sync_engine, if_exists='append', index=False
            )
            print("✅ CSV 데이터가 성공적으로 삽입되었습니다.")

        else:
            # 테이블이 존재하면 레코드 수 확인
            # 동기 작업을 비동기로 실행
            count = await asyncio.to_thread(
                lambda: sync_engine.execute(text("SELECT COUNT(*) FROM address")).scalar()
            )
            print(f"✅ address 테이블에 {count}개의 레코드가 있습니다. 초기화 스킵.")

    except Exception as e:
        print(f"❌ 초기화 중 오류 발생: {e}")
        # 실제 운영 환경에서는 앱 시작 실패하도록 raise 할 수도 있음
        # raise RuntimeError(f"Database initialization failed: {e}")


# --- FastAPI 이벤트 훅 (앱 시작/종료 시 실행) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작 시 실행
    print("🚀 FastAPI 시작!")
    await initialize_address_table()  # CSV 데이터 삽입 등
    yield
    # 앱 종료 시 실행
    print("👋 FastAPI 종료!")

app = FastAPI(title="Tobacco Retailer Location API", lifespan=lifespan)


# --- 좌표 변환 함수 ---
def convert_epsg5174_to_wgs84(x_5174, y_5174):
    """
    EPSG:5174 (Bessel 중부원점TM) 좌표를 EPSG:4326 (WGS84, 위도/경도)로 변환합니다.
    유효하지 않은 입력이나 변환 실패 시 (None, None)을 반환합니다.
    """
    # 입력 값이 NaN이거나 유효하지 않은지 확인 (pd.read_csv에서 NaN이 올 수 있음)
    if not isinstance(x_5174, (int, float)) or not isinstance(y_5174, (int, float)):
        return None, None
    if math.isnan(x_5174) or math.isnan(y_5174):
        return None, None
    
    # pyproj 내부에서 유효성 검사를 하므로, 여기서는 특이값(-1.0)만 처리
    # 만약 x,y가 0이거나 너무 작은 값 등 pyproj가 처리하지 못하는 값이 올 경우도 고려
    if x_5174 == -1.0 or y_5174 == -1.0: # CSV 처리 로직과 일관성 유지
        return None, None

    try:
        crs_5174 = pyproj.CRS("EPSG:5174")
        crs_4326 = pyproj.CRS("EPSG:4326")
        
        transformer = pyproj.Transformer.from_crs(crs_5174, crs_4326, always_xy=True)
        lon_4326, lat_4326 = transformer.transform(x_5174, y_5174)
        
        # 변환 결과가 NaN 또는 inf 인지 확인 (pyproj가 가끔 반환할 수 있음)
        if math.isnan(lat_4326) or math.isnan(lon_4326) or \
           math.isinf(lat_4326) or math.isinf(lon_4326):
            return None, None

        return lat_4326, lon_4326
    except pyproj.exceptions.ProjError as e:
        print(f"좌표 변환 중 ProjError 발생: x={x_5174}, y={y_5174}, Error: {e}")
        return None, None
    except Exception as e:
        print(f"알 수 없는 좌표 변환 오류 발생: x={x_5174}, y={y_5174}, Error: {e}")
        return None, None


# --- API 엔드포인트 ---

@app.get("/")
async def read_root():
    return {"message": "Welcome to Tobacco Retailer Location API!"}

@app.get("/test")
async def get_converted_addresses(db=Depends(get_db)):
    """
    DB에서 모든 주소의 x, y 좌표를 가져와 WGS84 (위도, 경도)로 변환하여 반환합니다.
    """
    print("🔄 /test 엔드포인트 호출: DB에서 좌표를 가져와 변환 중...")
    try:
        # DB에서 모든 주소 데이터 조회 (x, y, 주소 정보 포함)
        # pd.read_sql은 동기 함수이므로 asyncio.to_thread로 감싸서 실행
        query = text("SELECT landlot_address, road_name_address, x, y FROM address")
        df_addresses = await asyncio.to_thread(pd.read_sql, query, db.connection())

        if df_addresses.empty:
            return {"message": "데이터베이스에 주소 데이터가 없습니다."}

        # 각 행의 x, y 좌표를 WGS84로 변환
        # apply 또한 동기 함수이므로 asyncio.to_thread로 감싸서 실행
        df_addresses[['latitude', 'longitude']] = await asyncio.to_thread(
            df_addresses.apply,
            lambda row: convert_epsg5174_to_wgs84(row['x'], row['y']),
            axis=1,
            result_type='expand'
        )

        # 변환된 결과 정리
        converted_results = []
        for index, row in df_addresses.iterrows():
            if row['latitude'] is not None and row['longitude'] is not None:
                converted_results.append({
                    "landlot_address": row['landlot_address'],
                    "road_name_address": row['road_name_address'],
                    "original_x_5174": row['x'],
                    "original_y_5174": row['y'],
                    "converted_latitude_4326": row['latitude'],
                    "converted_longitude_4326": row['longitude']
                })
            else:
                converted_results.append({
                    "landlot_address": row['landlot_address'],
                    "road_name_address": row['road_name_address'],
                    "original_x_5174": row['x'],
                    "original_y_5174": row['y'],
                    "status": "변환 실패 (유효하지 않은 좌표)"
                })

        print(f"✅ 총 {len(converted_results)}개의 좌표 변환 완료.")
        return {"converted_addresses": converted_results}

    except Exception as e:
        print(f"❌ /test 엔드포인트 처리 중 오류 발생: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"좌표 변환 중 서버 오류 발생: {e}"
        )

@app.get("/check-location/{latitude}/{longitude}")
async def check_location_eligibility(
    latitude: float,
    longitude: float,
    db=Depends(get_db) # DB 연결 의존성 예시
):
    # 이 부분에서 OSMnx/GeoPandas를 사용하여 입지 분석 로직 구현
    # 예시: 현재는 무조건 '입점 가능'으로 반환
    print(f"Checking location: Lat={latitude}, Lon={longitude}")
    
    is_eligible = True # 실제 로직에 따라 변경
    
    if is_eligible:
        return {"status": "Access", "message": "해당 위치는 입점 가능합니다."}
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="해당 위치는 입점 제한 구역입니다.")

@app.get("/restricted-zones")
async def get_restricted_zones(db=Depends(get_db)):
    # 이 부분에서 모든 제한 구역 폴리곤 데이터를 반환하는 로직 구현
    # 예시: 더미 데이터 반환
    return {
        "status": "success",
        "zones": [
            # 실제 폴리곤 데이터 (GeoJSON 형식)
        ]
    }