"""논산 패밀리 런 — 실행: streamlit run app.py
GitHub에는 app.py / requirements.txt 두 파일을 올리세요.
설정 없이 체험 가능. 영구 공유는 앱 안의 배포 안내대로 Supabase 연결.
실시간/백그라운드 GPS 추적 및 도로 자동 길찾기는 제공하지 않습니다.
"""
from __future__ import annotations
import hashlib
import html
import io
import json
import math
import re
import secrets
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
import requests
import streamlit as st
from defusedxml import ElementTree as ET
from PIL import Image, ImageOps, UnidentifiedImageError
from streamlit_folium import st_folium

CENTER = [36.187247, 127.098744]  # 논산시청: https://www.findlatlng.org/place/nonsan-si
BUCKET = "family-exercise"
MAX_PHOTO = 10 * 1024 * 1024
MAX_GPX = 8 * 1024 * 1024
SQL = """-- Supabase SQL Editor에서 실행 (서버 전용 service_role 키 사용)
create table if not exists public.run_families (
 id text primary key, created_at timestamptz not null default now()
);
create table if not exists public.run_posts (
 id uuid primary key, family_id text not null references public.run_families(id),
 payload jsonb not null, created_at timestamptz not null default now()
);
create index if not exists run_posts_family_idx on public.run_posts(family_id, created_at desc);
alter table public.run_families enable row level security;
alter table public.run_posts enable row level security;
revoke all on public.run_families, public.run_posts from anon, authenticated;
grant select, insert on public.run_families to service_role;
grant select, insert, delete on public.run_posts to service_role;
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('family-exercise', 'family-exercise', false, 10485760, array['image/jpeg'])
on conflict (id) do nothing;
-- 기존 같은 이름의 버킷이 있다면 반드시 private로 설정하세요.
update storage.buckets set public = false where id = 'family-exercise';
-- anon/authenticated용 공개 접근 정책은 추가하지 않습니다.
"""


def distance_km(routes):
    total = 0.0
    for segment in routes:
        for a, b in zip(segment, segment[1:]):
            lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
            d = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
            total += 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, d))))
    return total


def parse_gpx(raw):
    if len(raw) > MAX_GPX:
        raise ValueError("GPX는 8MB 이하만 가능합니다.")
    root = ET.fromstring(raw)
    tag = lambda el: el.tag.rsplit('}', 1)[-1]
    if tag(root) != 'gpx':
        raise ValueError("GPX 형식이 아닙니다.")
    segments = [el for el in root.iter() if tag(el) == 'trkseg']
    point_tag = 'trkpt'
    if not segments:
        segments = [el for el in root.iter() if tag(el) == 'rte']
        point_tag = 'rtept'
    routes, count = [], 0
    for seg in segments:
        points = []
        for el in seg.iter():
            if tag(el) != point_tag:
                continue
            lat, lon = float(el.attrib['lat']), float(el.attrib['lon'])
            if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("GPX 좌표가 올바르지 않습니다.")
            points.append([lat, lon])
            count += 1
            if count > 15000:
                raise ValueError("GPX는 15,000개 지점까지 지원합니다. 짧은 운동 기록으로 나눠주세요.")
        if points:
            routes.append(points)
    if sum(map(len, routes)) < 2:
        raise ValueError("경로 지점이 2개 이상인 GPX가 필요합니다.")
    return routes


def photo_jpeg(raw):
    if len(raw) > MAX_PHOTO:
        raise ValueError("사진은 10MB 이하만 가능합니다.")
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.width * original.height > 25000000:
                raise ValueError("사진은 2,500만 화소 이하로 줄여주세요.")
            image = ImageOps.exif_transpose(original)
            image.thumbnail((1600, 1600))
            image = image.convert('RGB')
            # 새 이미지에 복사하여 EXIF/GPS 등 원본 메타데이터를 제거합니다.
            clean = Image.new('RGB', image.size)
            clean.paste(image)
            out = io.BytesIO()
            clean.save(out, format='JPEG', quality=85, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("읽을 수 없는 사진입니다. JPG 또는 PNG로 다시 올려주세요.") from exc


def token_valid(token):
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{24,128}', token))


def get_config():
    try:
        return str(st.secrets.get('SUPABASE_URL', '')).strip(), str(st.secrets.get('SUPABASE_SERVICE_ROLE_KEY', '')).strip()
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return '', ''


class Backend:
    def __init__(self, url, key, family):
        self.base = url.rstrip('/')
        self.family = family
        self.headers = {'apikey': key, 'Authorization': 'Bearer ' + key}

    def call(self, method, path, **kwargs):
        headers = dict(self.headers)
        headers.update(kwargs.pop('headers', {}))
        try:
            response = requests.request(method, self.base + path, headers=headers, timeout=25, **kwargs)
        except requests.RequestException:
            raise RuntimeError("저장소 연결이 끊겼습니다. 새로고침하여 저장 여부를 확인하세요.") from None
        if not response.ok:
            raise RuntimeError(f"저장소 오류 (HTTP {response.status_code}). Supabase Secrets·SQL·버킷 설정을 확인하세요.")
        return response.json() if response.content else None

    def exists(self):
        return bool(self.call('GET', '/rest/v1/run_families', params={'id': 'eq.' + self.family, 'select': 'id'}))

    def create(self):
        self.call('POST', '/rest/v1/run_families', json={'id': self.family})

    def posts(self):
        rows = self.call('GET', '/rest/v1/run_posts', params={'family_id': 'eq.' + self.family,
            'select': 'id,payload', 'order': 'created_at.desc', 'limit': '200'})
        return [dict(row['payload'], id=row['id']) for row in rows]

    def path(self, post_id):
        return f'{self.family}/{post_id}.jpg'

    def upload(self, post_id, image):
        self.call('POST', f'/storage/v1/object/{BUCKET}/{self.path(post_id)}',
            headers={'Content-Type': 'image/jpeg', 'x-upsert': 'false'}, data=image)

    def save(self, post, image):
        self.upload(post['id'], image)
        # ID가 고정되어 연결 재시도 시 중복 등록 여부를 확인할 수 있습니다.
        self.call('POST', '/rest/v1/run_posts', json={'id': post['id'], 'family_id': self.family, 'payload': post})

    def signed_photo(self, post_id):
        result = self.call('POST', f'/storage/v1/object/sign/{BUCKET}/{self.path(post_id)}', json={'expiresIn': 300})
        signed = result.get('signedURL') or result.get('signedUrl')
        if not signed:
            raise RuntimeError('사진 주소를 가져오지 못했습니다.')
        return self.base + '/storage/v1' + signed if signed.startswith('/') else signed

    def delete(self, post_id):
        # 사진 삭제 실패 시 게시글을 남겨 사용자가 재시도할 수 있도록 합니다.
        self.call('DELETE', f'/storage/v1/object/{BUCKET}', json={'prefixes': [self.path(post_id)]})
        self.call('DELETE', '/rest/v1/run_posts', params={'id': 'eq.' + post_id, 'family_id': 'eq.' + self.family})


def simplify(segment, limit=1200):
    # 표시는 간소화하되 저장/거리 계산에는 원본 지점을 사용합니다.
    if len(segment) <= limit:
        return segment
    step = math.ceil((len(segment)-1)/(limit-1))
    sampled = segment[::step]
    if sampled[-1] != segment[-1]:
        sampled.append(segment[-1])
    return sampled


def make_map(routes=None, posts=None):
    m = folium.Map(location=CENTER, zoom_start=13, tiles='OpenStreetMap', control_scale=True)
    folium.TileLayer('CartoDB positron', name='밝은 지도').add_to(m)
    folium.TileLayer('CartoDB dark_matter', name='어두운 지도').add_to(m)
    folium.LatLngPopup().add_to(m)
    folium.plugins.Fullscreen(position='topright').add_to(m)
    bounds = []
    for seg in routes or []:
        if len(seg) > 1:
            folium.PolyLine(simplify(seg), color='#0d9488', weight=5, opacity=.9, tooltip='운동 경로').add_to(m)
        bounds.extend(seg)
    if routes:
        first, last = routes[0][0], routes[-1][-1]
        folium.Marker(first, tooltip='출발', icon=folium.Icon(color='green', icon='play')).add_to(m)
        folium.Marker(last, tooltip='도착 · 인증사진 위치', icon=folium.Icon(color='red', icon='flag')).add_to(m)
    for post in posts or []:
        end = post['routes'][-1][-1]
        popup = f"<b>{html.escape(post['name'])}</b><br>{html.escape(post['title'])}<br>{post['km']:.2f} km<br>{html.escape(post['message'])}<br>사진은 아래 기록 카드에서 확인하세요."
        folium.Marker(end, popup=folium.Popup(popup, max_width=280), tooltip=html.escape(post['title']),
            icon=folium.Icon(color='blue', icon='camera')).add_to(m)
        bounds.append(end)
    if bounds:
        m.fit_bounds([[min(p[0] for p in bounds), min(p[1] for p in bounds)],
                      [max(p[0] for p in bounds), max(p[1] for p in bounds)]], max_zoom=16)
    folium.LayerControl().add_to(m)
    return m


from folium import plugins

st.set_page_config(page_title='논산 패밀리 런', page_icon='🏃', layout='wide')
st.markdown('''<style>
.block-container {max-width:1180px; padding-top:1.6rem;}
[data-testid="stMetric"] {border:1px solid #80908055; border-radius:16px; padding:14px;}
.stButton>button {min-height:44px; border-radius:10px;}
</style>''', unsafe_allow_html=True)
st.title('🏃 논산 패밀리 런')
st.caption('논산에서 함께 움직이고, 도착지에 추억을 남겨요. 지도 · 운동 거리 · 가족 인증사진')

with st.expander('📖 처음 사용 / GitHub·Streamlit 배포 / 사진 저장 설정'):
    st.markdown('''### 두 파일로 배포하기
1. GitHub 저장소에 **app.py**, **requirements.txt**를 업로드합니다.
2. Streamlit Community Cloud에서 저장소와 메인 파일 `app.py`를 선택해 배포합니다.
3. 설정이 없으면 세션 한정 체험 모드입니다. **GitHub에는 사진·운동 데이터가 저장되지 않습니다.**

### 가족 공유·영구 저장 설정
1. Supabase 프로젝트를 만들고 SQL Editor에서 아래 SQL을 실행하세요.
2. Streamlit 앱의 Settings → Secrets에 프로젝트 URL과 **service_role 키**를 입력하고 재시작하세요.
3. 가족 공간을 만들고 앱 주소와 초대 코드를 가족에게만 전달하세요.
4. 사진은 비공개 Storage, 운동 기록은 Postgres에 저장됩니다. 사진 주소는 5분 만료입니다.

**보안:** service_role 키를 GitHub에 올리지 마세요. 서버의 Secrets에만 넣으세요.
초대 코드는 개인 계정 대신 쓰는 공유 비밀번호입니다. 코드를 아는 사람은 기록 열람·추가·삭제가 가능합니다.
코드는 복구할 수 없으므로 안전하게 보관하고, 유출되면 새 공간으로 옮기세요.
가족별 개인 권한, 초대 코드 회전, 전체 가족 공간 삭제 기능은 없습니다. 관리자는 Supabase에서 정리할 수 있습니다.

### 경로·지도 안내
- **지도 직접 선택**: 이동 순서대로 경유지를 찍으면 지점 사이 직선거리를 더합니다. 실제 도로 길찾기 거리가 아닙니다.
- **GPX 가져오기**: 운동 기록 앱에서 내보낸 GPS 트랙을 올리면 좌표 간 거리 합을 계산합니다. 고도는 반영하지 않습니다.
- 경로 중단 구간(GPX segment)은 연결하지 않습니다. GPS 오차가 있으면 거리도 달라질 수 있습니다.
- 휴대폰 화면을 끈 상태의 GPS 자동 추적, 걸음 수, 내비게이션, 자동 운동 인증은 지원하지 않습니다.
- 지도는 논산시청 중심으로 시작합니다. 확대하여 도로·산책로를 볼 수 있지만 모든 시설·길의 최신성을 보장하지 않습니다.
- 외부 지도 서비스에 지도 영역 요청이 전송됩니다. 지도 타일은 제공자의 정책·가용성에 따라 제한될 수 있습니다.
- 사진 EXIF/GPS 메타데이터는 제거하지만 지도 경로와 도착 좌표는 가족에게 공유됩니다. 집·학교 위치와 얼굴 노출에 주의하세요.
- 최신 200개 기록을 불러옵니다. 통계는 불러온 기록 기준입니다.
- 드물게 사진 업로드 후 기록 저장이 실패하면 연결되지 않은 사진이 남을 수 있습니다. 관리자 Storage에서 정리하세요.
''')
    st.code(SQL, language='sql')
    st.code('SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"\nSUPABASE_SERVICE_ROLE_KEY = "YOUR_SERVICE_ROLE_KEY"', language='toml')
    st.caption('로컬: pip install -r requirements.txt → streamlit run app.py. 로컬 Secrets는 .streamlit/secrets.toml에 저장하고 커밋하지 마세요.')

url, key = get_config()
if bool(url) != bool(key):
    st.error('SUPABASE_URL과 SUPABASE_SERVICE_ROLE_KEY를 모두 설정하세요.')
    st.stop()
cloud = bool(url and key)
if not cloud:
    st.warning('체험 모드: 현재 접속 세션에만 저장됩니다. 가족 간 공유·영구 보관은 Supabase 연결 후 가능합니다.')

with st.sidebar:
    st.header('👨‍👩‍👧 가족 공간')
    if cloud:
        if not st.session_state.get('run_token'):
            with st.form('join'):
                token = st.text_input('가족 초대 코드', type='password')
                if st.form_submit_button('가족 참여', use_container_width=True):
                    token = token.strip()
                    if not token_valid(token):
                        st.error('초대 코드 형식을 확인하세요.')
                    else:
                        try:
                            backend = Backend(url, key, hashlib.sha256(token.encode()).hexdigest())
                            if backend.exists():
                                st.session_state.run_token = token
                                st.rerun()
                            else:
                                st.error('해당 가족 공간이 없습니다.')
                        except RuntimeError as exc:
                            st.error(str(exc))
            if st.button('새 가족 공간 만들기', use_container_width=True):
                token = secrets.token_urlsafe(24)
                try:
                    Backend(url, key, hashlib.sha256(token.encode()).hexdigest()).create()
                    st.session_state.run_token = token
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
        else:
            with st.expander('가족 초대 코드'):
                st.code(st.session_state.run_token, language=None)
            if st.button('가족 공간 나가기'):
                # 이전 가족의 사진/작성 내용을 세션에 남기지 않습니다.
                st.session_state.clear()
                st.rerun()
    st.caption('담당 이름은 본인 인증 기능이 아닙니다. 신뢰하는 가족끼리만 사용하세요.')
    if st.button('↻ 최신 기록 불러오기', use_container_width=True):
        st.rerun()

if cloud and not st.session_state.get('run_token'):
    st.info('왼쪽 메뉴에서 가족 공간을 만들거나 초대 코드로 참여하세요. 모바일에서는 상단 사이드바 버튼을 누르세요.')
    st.stop()
backend = Backend(url, key, hashlib.sha256(st.session_state.run_token.encode()).hexdigest()) if cloud else None
try:
    posts = backend.posts() if cloud else st.session_state.setdefault('run_demo_posts', [])
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()
if st.session_state.get('run_notice'):
    st.success(st.session_state.pop('run_notice'))

c1, c2, c3 = st.columns(3)
c1.metric('가족 운동 기록', f'{len(posts)}회')
c2.metric('기록 거리 합계', f"{sum(p['km'] for p in posts):.2f} km")
c3.metric('참여 이름', f"{len(set(p['name'] for p in posts))}명")
st.caption('지도 선택 거리와 GPS 기록 거리가 함께 집계됩니다. 최신 200개 기준이며 실제 운동 여부를 검증하는 수치는 아닙니다.')

write_tab, family_tab = st.tabs(['📍 운동 인증 남기기', '🗺️ 가족 운동 지도·사진첩'])
with write_tab:
    st.subheader('1. 운동 경로 만들기')
    mode = st.radio('거리 입력 방식', ['지도에서 경유지 선택', 'GPX 파일 가져오기'], horizontal=True)
    routes, source = [], ''
    if mode == '지도에서 경유지 선택':
        points = st.session_state.setdefault('run_points', [])
        st.caption('지도에서 위치를 클릭 → 아래 “선택한 위치 추가”를 누르세요. 이동 순서대로 출발·경유·도착 지점을 추가합니다. 마지막 점이 인증사진 도착지입니다.')
        result = st_folium(make_map([points] if points else []), height=470, use_container_width=True,
            key='route_picker', returned_objects=['last_clicked'])
        chosen = (result or {}).get('last_clicked')
        b1, b2, b3 = st.columns(3)
        if b1.button('📌 선택한 위치 추가', disabled=not chosen, use_container_width=True):
            if len(points) >= 500:
                st.error('경유지는 500개까지 가능합니다.')
            else:
                point = [chosen['lat'], chosen['lng']]
                if points and point == points[-1]:
                    st.warning('마지막 지점과 같습니다. 다른 곳을 클릭하세요.')
                else:
                    points.append(point)
                    st.rerun()
        if b2.button('마지막 점 취소', disabled=not points, use_container_width=True):
            points.pop()
            st.rerun()
        if b3.button('경로 전체 초기화', disabled=not points, use_container_width=True):
            st.session_state.run_points = []
            st.rerun()
        if chosen:
            st.caption(f"선택 위치: {chosen['lat']:.6f}, {chosen['lng']:.6f}")
        if len(points) >= 2:
            routes, source = [points.copy()], '지도 경유지 직선거리'
        st.caption(f'현재 경유지 {len(points)}개 · 출발과 도착을 포함해 2개 이상 필요합니다.')
    else:
        gpx = st.file_uploader('운동 앱에서 내보낸 GPX 파일', type=['gpx'], key='gpx_file')
        if gpx:
            try:
                routes = parse_gpx(gpx.getvalue())
                source = 'GPX 좌표 계산 거리'
                st_folium(make_map(routes), height=440, use_container_width=True, key='gpx_preview', returned_objects=[])
                st.caption(f'{sum(map(len, routes)):,}개 GPS 지점 · {len(routes)}개 구간')
            except Exception as exc:
                # 업로드 파일의 내부 텍스트/서버 상세는 노출하지 않습니다.
                st.error(str(exc) if isinstance(exc, ValueError) else 'GPX를 읽을 수 없습니다. 표준 GPX 파일을 다시 올려주세요.')
    km = distance_km(routes)
    if routes:
        st.info(f'📏 {km:.2f} km · {source} | 도착 좌표: {routes[-1][-1][0]:.6f}, {routes[-1][-1][1]:.6f}')
    st.subheader('2. 도착지 사진과 메시지')
    st.caption('사진은 업로드 또는 촬영 중 하나를 선택하세요. 사진 촬영은 브라우저 카메라 권한이 필요합니다. HEIC는 JPG로 변환해주세요.')
    image_mode = st.radio('사진 방법', ['사진 파일 업로드', '지금 사진 촬영'], horizontal=True)
    image_file = st.file_uploader('인증사진 (JPG/PNG, 최대 10MB)', type=['jpg','jpeg','png'], key='run_photo') if image_mode == '사진 파일 업로드' else st.camera_input('도착지 인증사진 촬영')
    if image_file:
        st.image(image_file, caption='저장 전 사진 확인', width=300)
    with st.form('publish'):
        name = st.text_input('이름 또는 별명', max_chars=24)
        title = st.text_input('운동 제목', max_chars=80, placeholder='예: 가족과 논산 저녁 산책')
        kind = st.selectbox('운동 종류', ['걷기', '달리기', '자전거', '기타'])
        exercise_date = st.date_input('운동 날짜', value=datetime.now(ZoneInfo('Asia/Seoul')).date())
        minutes = st.number_input('운동 시간 (분, 직접 입력)', min_value=1, max_value=1440, value=30, step=1)
        message = st.text_area('도착지 메시지', max_chars=1000, placeholder='도착! 오늘도 함께해서 즐거웠어요.')
        agree = st.checkbox('경로·도착 위치·사진·메시지를 가족에게 공유하는 데 동의합니다.')
        submitted = st.form_submit_button('🏁 운동 인증 저장', type='primary', use_container_width=True)
    if submitted:
        if not routes or sum(map(len, routes)) < 2:
            st.error('먼저 경로를 지정하거나 GPX 파일을 올려주세요.')
        elif not name.strip() or not title.strip() or not message.strip():
            st.error('이름, 운동 제목, 도착지 메시지를 모두 입력하세요.')
        elif not image_file:
            st.error('인증사진을 올리거나 촬영하세요.')
        elif not agree:
            st.error('가족 공유 동의를 확인하세요.')
        elif exercise_date > datetime.now(ZoneInfo('Asia/Seoul')).date():
            st.error('미래 날짜로 인증을 등록할 수 없습니다.')
        elif not cloud and len(posts) >= 50:
            st.error('체험 모드는 50개까지 가능합니다. 기존 기록을 삭제하거나 공유 저장소를 연결하세요.')
        else:
            try:
                image = photo_jpeg(image_file.getvalue())
                post = {'id':str(uuid.uuid4()), 'name':name.strip(), 'title':title.strip(), 'kind':kind,
                    'date':exercise_date.isoformat(), 'minutes':int(minutes), 'message':message.strip(),
                    'km':round(km, 6), 'source':source, 'routes':routes,
                    'created_at':datetime.now(ZoneInfo('Asia/Seoul')).isoformat()}
                if cloud:
                    backend.save(post, image)
                else:
                    post['image'] = image
                    st.session_state.run_demo_posts.insert(0, post)
                st.session_state.run_notice = '운동 인증을 저장했어요! 가족 운동 지도·사진첩 탭에서 확인하세요.'
                st.rerun()
            except (RuntimeError, ValueError) as exc:
                st.error(str(exc))

with family_tab:
    st.subheader('가족의 도착지가 모이는 지도')
    names = sorted(set(p['name'] for p in posts))
    selection = st.selectbox('가족 이름 필터', ['전체'] + names)
    filtered = [p for p in posts if selection == '전체' or p['name'] == selection]
    if filtered:
        labels = {p['id']: f"{p['date']} · {p['name']} · {p['title']}" for p in filtered}
        selected_id = st.selectbox('경로 자세히 보기', ['전체 도착지'] + list(labels), format_func=lambda x: labels.get(x, x))
        selected = next((p for p in filtered if p['id'] == selected_id), None)
        st_folium(make_map(selected['routes'] if selected else None, [selected] if selected else filtered),
            height=490, use_container_width=True, key='family_map', returned_objects=[])
        st.caption('지도 카메라 마커를 누르면 도착지 메시지가 보입니다. 해당 사진은 아래 기록을 펼쳐 확인하세요.')
        export = [{k:v for k,v in p.items() if k != 'image'} for p in filtered]
        st.download_button('현재 기록 JSON 백업 (사진 제외)', json.dumps(export, ensure_ascii=False, indent=2),
            file_name='family_runs.json', mime='application/json')
        st.caption('백업에는 상세 경로·도착 좌표가 포함됩니다. 외부 공개에 주의하세요.')
        st.subheader('📸 도착지 사진첩')
        for post in filtered:
            with st.expander(f"{post['date']} · {post['name']} · {post['title']} · {post['km']:.2f}km"):
                st.write(post['message'])
                st.caption(f"{post['kind']} · {post['minutes']}분(직접 입력) · {post['source']}")
                st.caption(f"도착: {post['routes'][-1][-1][0]:.6f}, {post['routes'][-1][-1][1]:.6f}")
                # URL 서명은 필요한 사진에 대해서만 만듭니다.
                if st.button('인증사진 보기', key='view_' + post['id']):
                    try:
                        if cloud:
                            st.image(backend.signed_photo(post['id']), use_container_width=True)
                        else:
                            st.image(post['image'], use_container_width=True)
                    except RuntimeError as exc:
                        st.error(str(exc))
                confirmed = st.checkbox('사진과 기록을 영구 삭제합니다', key='confirm_' + post['id'])
                if st.button('이 인증 삭제', disabled=not confirmed, key='delete_' + post['id']):
                    try:
                        if cloud:
                            backend.delete(post['id'])
                        else:
                            st.session_state.run_demo_posts = [p for p in posts if p['id'] != post['id']]
                        st.session_state.run_notice = '기록과 사진을 삭제했습니다.'
                        st.rerun()
                    except RuntimeError as exc:
                        st.error(str(exc))
    else:
        st_folium(make_map(), height=450, use_container_width=True, key='empty_map', returned_objects=[])
        st.info('아직 기록이 없어요. 첫 운동 인증을 남겨보세요!')

st.caption('논산 패밀리 런 · 한국 시간 기준 · 지도: OpenStreetMap / CARTO · 안전한 곳에서 사진을 촬영하고 이동 중 휴대폰 조작은 피하세요.')
