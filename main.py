# -*- coding: utf-8 -*-

from urllib.request import urlopen
from urllib.parse import urljoin, urlparse, parse_qsl
from datetime import timedelta, datetime
import re
import sys
import subprocess
import multiprocessing
import tempfile
import ctypes
import os

from scapy.all import *
from bs4 import BeautifulSoup
import platform

FFMPEG_BIN = ""
rejected_urls = []
rejected_video_codes = []
video = None


class Video():
    def __init__(self, ffmpeg_bin):
        self.ffmpeg_bin = ffmpeg_bin
        self.url = None
        self.title = None
        self.file_name = None
        self.video_code = None
        self.length = None
        self.acc_length = timedelta()
        self.video_parts = list()
        self.video_selected = False
        self.first_search = True

        self.start_download = False
        self.broadcast_date = None

    def is_collecting_done(self, packet):
        """
            collecting is done if difference between correct length
            and collected length is less than double of number of video parts
        """

        return self.start_download
        """
        if self.length == None:
            return False
        else:
            diff_length = abs((self.length-self.acc_length).total_seconds()) 
            return diff_length < 2*len(self.video_parts)
        """
    @staticmethod
    def parse_video_code_from_html(soup):
        """
            [Video Code]
            다시보기:
                http://###/SnapshotLoad.php?rowKey=20190517_1E64A22B_214079130_2_r
                    => 214079130
            하이라이트:
                http://###/SnapshotLoad.php?rowKey=20190104_AD32A5D7_210123806_5_164185_h_r
                    => 210123806
            VOD:
                http://###/2019/0417/18/thumb/1555491900539235_L_7.jpg
                    => 1555491900539235
        """

        video_code = soup.find("meta", property="og:image")["content"]
        if '/save/' in video_code:
            return re.search("/[0-9]+_", video_code).group(0).strip('/_')
        else:
            return re.search("_[A-Z0-9]+_[0-9]+_", video_code).group(0).split('_')[-2]
        
    @staticmethod
    def parse_video_code_from_url(url):
        """
            [Video Code]
            다시보기:
                http://###/smil:vod/20190516/800/4491EA42_214065800_1.smil/media_b2000000_t64aGQyaw==_5.ts
                    => 214065800
            하이라이트:
                http://###/smil:highlight/20190112/002/38C5568D_210358002_5_168045.smil/media_b7953000_t64b3JpZ2luYWw=_0.ts
                    => 210358002
            VOD:
                http://###/mp4:save/afreeca/station/2019/0504/01/1556901150709495.mp4/media_w133379925_2.ts
                    => 1556901150709495
        """

        if 'save' in url:
            return re.search("/[0-9]+\.", url).group(0).strip('/.')
        else:
            return re.search("/[A-Z0-9]+_[0-9]+_", url).group(0).split('_')[-2]

    def get_video_info(self, url):
        self.url = url
        
        html = urlopen(url).read()
        soup = BeautifulSoup(html, 'html.parser')

        self.title = soup.find(id="title_name").text.strip()
        
        ### 방송일
        #<strong>방송시간</strong><span>2019-09-06 19:08:12 ~ 2019-09-07 02:46:42
        #<strong>방송 시작일</strong><span>2019-01-03 19:29:13</span></li>
        date_soup = soup.find(id="vodDetailView").select("li")[0]
        item_name = date_soup.find("strong")
        if (item_name
            and ("방송시간" in item_name or "방송 시작일" in item_name)):
            date = date_soup.find("span").text.split()[0]
            self.broadcast_date = datetime.strptime(date, "%Y-%m-%d")
        
        file_name = str(self.title).strip().replace(' ', '_')
        if self.broadcast_date:
            self.file_name = (self.broadcast_date.strftime("%y%m%d") + " " +
                             re.sub(r'(?u)[^-\w.]', '', file_name) + ".mp4")
        else:
            self.file_name = re.sub(r'(?u)[^-\w.]', '', file_name) + ".mp4"

        self.video_code = Video.parse_video_code_from_html(soup)

    def get_video_length(self, path):
        """
            Get video length from HTTP GET Path

            path: http://afbbs.afreecatv.com:8080/api/video/set_vout_log.php?###&duration=15162&quality=###
                => 15162 sec
        """

        parsed = urlparse(path)
        length_seconds = dict(parse_qsl(parsed.query))['duration']
        length_seconds =  int(float(length_seconds))
        self.length = timedelta(seconds=length_seconds)
    
    def download(self):
        # Download video parts
        pool = multiprocessing.Pool(processes=None)
        pool.map(VideoPart.download, self.video_parts)
        pool.close()
        pool.join()

        # If video consists of one file, just remove 'part*' from file name
        #   ex) '### part1.mp4' -> '###.mp4'
        # If video consists of several files, concat using ffmpeg
        #   ex) '### part1.mp4' + ... + '### part4.mp4 -> '###.mp4'

        if len(self.video_parts) == 1:
            only_part = self.video_parts[0]
            idx = only_part.file_name.rfind(" part")
            new_name = only_part.file_name[:idx] + ".mp4"
            os.rename(only_part.file_name, new_name)

        else:
            fd, path = tempfile.mkstemp()
            try:
                # Make temp file for ffmpeg input
                with os.fdopen(fd, 'w') as tmp:
                    for part in self.video_parts:
                        # If file name is "abc.mp4", 
                        # line in temp file is "file '/foo/bar/abc.mp4'"

                        part_file_path = urljoin(os.path.abspath(__file__), part.file_name) 
                        line = "file \'" + part_file_path + '\'\n'
                        tmp.write(line)

                command = [self.ffmpeg_bin,
                            '-f', 'concat',
                            '-safe', '0',
                            '-i', path,
                            '-c', 'copy',
                            '-y', self.file_name]
                subprocess.run(command)
                
                # Remove original file
                with open(path, 'r') as tmp:
                    for line in tmp:
                        part_file_name = line[len("file '"):-1]
                        os.remove(part_file_name)
                
            finally:
                sys.stdout.flush()
                os.remove(path)

class VideoPart():
    def __init__(self, url, concat_file_name, ffmpeg_bin):
        self.url = url
        self.ffmpeg_bin = ffmpeg_bin

        # 다시보기 -> smil:vod, smil:mvod(part exists)
        # 하이라이트 -> smil:highlight(part does not exist)
        # 업로드 VOD, 유저 VOD ->  smil:save(part does not exist)

        if "smil:vod" in self.url:
            tmp_part_no = re.search("/[A-Z0-9]+_[0-9]+_[0-9]+", self.url)
            tmp_part_no = tmp_part_no.group(0)
            self.part_no = tmp_part_no.rpartition("_")[2]
        else:
            self.part_no = 1

        self.file_name = concat_file_name.replace('.mp4', '')
        self.file_name += " part" + str(self.part_no) + '.mp4'

        self.length = .0
        self.chunk_cnt = 0
        self.calc_length_and_chunk_cnt()

    def calc_length_and_chunk_cnt(self):
        """
            ex) playlist file(m3u8)
            #EXTM3U
            #EXT-X-VERSION:3
            #EXT-X-STREAM-INF:BANDWIDTH=8000000,NAME="original"
            http://###/FBC6765E_213027651_1.smil/chunklist_b8000000_t64b3JpZ2luYWw=.m3u8
            ...

            ex) chunklist file(m3u8)
            #EXTINF:4.0,
            media_b8000000_t64b3JpZ2luYWw=_0.ts
            #EXTINF:4.0,
            media_b8000000_t64b3JpZ2luYWw=_1.ts
            ...

            Add all float number next to the '#EXTINF'
        """

        playlist_lines = urlopen(self.url).readlines()
        playlist_lines = [l.decode('utf-8') for l in playlist_lines]

        for i, line1 in  enumerate(playlist_lines):
            if "NAME=\"original\"" in line1:
                chunklist_url = playlist_lines[i+1]
                chunklist = urlopen(chunklist_url)
                
                length = .0
                for line2 in chunklist.readlines():
                    line2 = line2.decode("utf-8")
                    if line2.startswith("#EXTINF:"):
                        length += float(line2.strip()[len("#EXTINF:"):-len(",")])
                        self.chunk_cnt += 1
                self.length = timedelta(seconds=round(length))
                print("\tpart_length:", self.length)
                break
  
    def download(self):
        print("Downloading", self.file_name, "...")

        try:
            command = [self.ffmpeg_bin,
                        '-i', self.url,
                        '-c', 'copy',
                        '-bsf:a', 'aac_adtstoasc',
                        '-y', self.file_name]

            subprocess.run(command)
        finally:
            sys.stdout.flush()

def extract_host(packet):
    """
        This function return host name form http request
        
        ex)
        GET /###
        Host: [host name]
        ...
    """

    http_msg = str(packet[Raw].fields["load"]).split(r"\r\n")
    for line in http_msg:
        if line.startswith("Host:"):
            host_name = line[len("Host:"):].strip()
    return host_name

def collect_playlist(packet):
    global rejected_urls, rejected_video_codes
    packet_str = str(packet)

    # Find path at HTTP GET packet
    start_idx = packet_str.find("GET") + len("GET")
    end_idx = packet_str.find("HTTP")
    path = packet_str[start_idx: end_idx].strip()
    
    ## Find host at HTTP GET packet
    host = extract_host(packet)
    url = "http://" + host + path

    if not video.video_selected and ".ts" in path:
        tmp_video_code =  Video.parse_video_code_from_url(path)
        if tmp_video_code in rejected_video_codes:
            pass
        else:
            print("[동영상 감지. 새로고침이 필요합니다]")
        return

    elif (not video.video_selected and not url in rejected_urls
            and url.startswith("http://vod.afreecatv.com/PLAYER/STATION/")):
        video.get_video_info(url)
        print("[동영상 정보]")
        print("\ttitle:", video.title)
        print("\t방송일:", video.broadcast_date.strftime("%Y-%m-%d"))
        print("\turl:", video.url)
        print("\tvideo_code:", video.video_code)

        while True:
            answer = input("\tDownload? [y/n]: ")
            if answer.lower() == 'y':
                video.video_selected = True
                break

            elif answer.lower() == 'n':
                rejected_urls.append(video.url)
                rejected_video_codes.append(video.video_code)
                video.video_selected = False
                break

            else:
                continue

    elif video.video_selected and video.first_search and path.startswith("/api/video/set_vout_log.php?"):
        video.first_search = False
        video.get_video_length(path)
        return

    elif video.video_selected and not video.first_search and ".ts" in packet_str:
        part_url = urljoin(url, "playlist.m3u8")
        tmp_video_code = Video.parse_video_code_from_url(part_url)

        if (video.video_code == tmp_video_code and
            part_url not in [part.url for part in video.video_parts]):
            print("[새로운 part 발견]")
            print("\turl:", part_url)
            print("\ttotal_length:", video.length)
            
            part = VideoPart(part_url, video.file_name, video.ffmpeg_bin)
            video.video_parts.append(part)
            video.acc_length += part.length

            print("\tremain_length:", video.length - video.acc_length)
            
            while True:
                answer = input("\tDownload? [y/n]: ")
                if answer.lower() == 'y':
                    video.start_download = True
                    break

                elif answer.lower() == 'n':
                    break

                else:
                    continue
        
        
        return

def check_os():
    global FFMPEG_BIN

    if platform.system() == "Linux":
        FFMPEG_BIN = "ffmpeg"
    elif platform.system() == "Windows":
        FFMPEG_BIN = "ffmpeg.exe"
        multiprocessing.set_start_method('spawn')
    else:
        print(platform.system() + "is not supported")
        sys.exit(-1)

def check_admin():
    try:
        is_admin = os.getuid() == 0
    except AttributeError:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        print(is_admin)
        exit()
        sys.exit(-1)
    if not is_admin:
        exit()
    
def main():
    global video
    man = """
[사용 방법]
    1. 프로그램 실행 python main.py
    2. 웹브라우저를 통해 동영상이 있는 웹 페이지로 이동한 뒤 새로고침 한다.
        ex) http://vod.afreecatv.com/PLAYER/STATION/###
    3. 전체 동영상은 보통 1시간 단위의 파트로 나누어져 있지만
       그보다 작은 파트도 존재한다.
       재생 바의 여러 부분을 클릭하여 모든 파트를 찾아야 한다.
       파트를 찾게 되면 메세지가 표시되며 모든 파트를 발견한 경우 자동으로 다운로드가 시작된다."""
    
    check_os()
    check_admin()
    video = Video(FFMPEG_BIN)
    
    print(man)

    sniff(prn=collect_playlist, stop_filter=video.is_collecting_done, 
            lfilter=lambda p: 'GET /' in str(p), filter="tcp")
    
    if video.is_collecting_done(None):
        video.download()

if __name__ == "__main__":
    main()
