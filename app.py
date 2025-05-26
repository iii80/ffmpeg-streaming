from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import subprocess
import os
import signal
from datetime import datetime, timedelta
import psutil
from urllib.parse import urlparse, urlunparse, urljoin
import random
import shlex
from threading import Thread, Lock, Event, current_thread
import threading
import time
import logging
import traceback
import pytz

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

# 设置时区
TIMEZONE = pytz.timezone('Asia/Shanghai')

def get_shanghai_time():
    """获取上海时区的当前时间"""
    return datetime.now(TIMEZONE).replace(tzinfo=None)

app = Flask(__name__)

# 全局变量初始化
running_streams = {} 
running_streams_lock = Lock()
running_tasks = {}  # 用于存储运行中的定时任务

HISTORY_FILE = "stream_history.log"
RTMP_SERVER = "rtmp://ali.push.yximgs.com/live/"
SUPPORTED_PROTOCOLS = {
    "hls": {
        "prefix": "/hls/",
        "scheme": "http"
    },
    "udp": {
        "prefix": "/udp/",
        "scheme": "udp"
    },
    "rtp": {
        "prefix": "/rtp/",
        "scheme": "rtp"
    },
    "file": {
        "prefix": "/file/",
        "scheme": "file"
    },
    "rtsp": {
        "prefix": "/rtsp/",
        "scheme": "rtsp"
    },
    "rtmp": {
        "prefix": "/rtmp/",
        "scheme": "rtmp"
    }
}

class StreamProcessor:
    def __init__(self, input_source, stream_id, protocol, buffered_mode=False):
        self.protocol = protocol
        self.stream_id = stream_id
        self.buffered_mode = buffered_mode
        self.rtmp_url = f"{RTMP_SERVER}{stream_id}"
        self.last_restart_time = None
        self.restart_count = 0
        self.max_restarts_per_hour = 10  # 每小时最大重启次数
        self.input_source = input_source
        self.extra_params = self._get_extra_params()
        self.process = None
        self.error_log = None  # 存储FFmpeg错误信息
        self._stop_event = threading.Event()

    def _parse_path_url(self, url, protocol):
        parsed = urlparse(url)
        path_prefix = SUPPORTED_PROTOCOLS.get(protocol, {}).get("prefix", "")
        if parsed.path.startswith(path_prefix):
            target = parsed.netloc + parsed.path[len(path_prefix):]
            return f"{protocol}://{target}"
        return url

    def _get_extra_params(self):
        params = []
        
        # 为UDP/RTP流设置特定参数
        if self.protocol in ["udp", "rtp"]:
            params.extend([
                "-max_delay", "5000000",   # 最大延迟5秒
                "-bufsize", "16000k",       # 增大缓冲区大小
                "-flags", "+global_header", # 添加全局头部
                "-rw_timeout", "5000000",   # 设置读写超时时间
                "-probesize", "32M",        # 增大探测大小
                "-analyzeduration", "10000000" # 增加分析持续时间
            ])
        elif self.protocol == "hls":
            # HLS特定参数
            params.extend([
                "-hls_time", "6",
                "-hls_flags", "append_list+omit_endlist",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10"
            ])

        # 视频编码参数
        params.extend([
            "-c:v", "copy",
        ])

        # 音频编码参数
        params.extend([
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100"
        ])

        # 码率控制参数
        params.extend([
            "-maxrate", "1500k",
            "-minrate", "1000k",
            "-g", "50"
        ])

        if self.buffered_mode:
            params.extend([
                "-flvflags", "no_duration_filesize",
                "-avioflags", "direct",
                "-max_muxing_queue_size", "1024",
                "-tune", "zerolatency",
                "-muxdelay", "0.1",
                "-muxpreload", "0.1"
            ])
            
        return params

    def can_restart(self):
        """检查是否允许重启"""
        now = datetime.now()
        if self.last_restart_time is None:
            return True
            
        # 重置计数器（如果已经过了一小时）
        if (now - self.last_restart_time).total_seconds() > 3600:
            self.restart_count = 0
            return True
            
        # 检查是否超过每小时最大重启次数
        return self.restart_count < self.max_restarts_per_hour

    def start(self):
        """启动流处理器"""
        if not self.can_restart():
            return None, "已达到最大重启次数限制，请等待一小时后再试"

        # 构建FFmpeg命令
        cmd = [
            "ffmpeg",
            "-re",
            "-i", self.input_source,
            "-f", "flv"
        ]

        # 添加额外参数
        cmd.extend(self.extra_params)
        cmd.append(self.rtmp_url)

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True
            )
            
            # 更新重启计数
            self.last_restart_time = datetime.now()
            self.restart_count += 1
            
            # 异步读取错误输出
            def read_errors():
                self.error_log = self.process.stderr.read().decode('utf-8')

            error_thread = threading.Thread(target=read_errors)
            error_thread.daemon = True
            error_thread.start()

            return self.process.pid, None
        except Exception as e:
            self.error_log = str(e)
            return None, str(e)
            
    def stop(self):
        """停止推流进程及其所有子进程"""
        try:
            if not self.process:
                return True

            self._stop_event.set()  # 设置停止标志

            # 获取进程组ID
            if hasattr(os, 'getpgid'):
                try:
                    pgid = os.getpgid(self.process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

            # 使用 SIGTERM 信号终止进程
            try:
                self.process.terminate()
                # 等待进程终止，最多等待5秒
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 如果进程没有响应 SIGTERM，使用 SIGKILL
                    self.process.kill()
                    self.process.wait(timeout=2)
            except (ProcessLookupError, OSError):
                pass

            # 确保进程已经完全终止
            if psutil.pid_exists(self.process.pid):
                try:
                    proc = psutil.Process(self.process.pid)
                    # 终止所有子进程
                    for child in proc.children(recursive=True):
                        try:
                            child.terminate()
                            child.wait(timeout=2)
                        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                    # 终止主进程
                    if proc.is_running():
                        proc.terminate()
                        proc.wait(timeout=2)
                        if proc.is_running():
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    pass

            self.process = None
            return True
        except Exception as e:
            logging.error(f"Error stopping process: {str(e)}")
            return False

def monitor_streams():
    while True:
        time.sleep(5)  # 每5秒检查一次
        with running_streams_lock:
            try:
                # 遍历副本避免字典修改异常
                for pid in list(running_streams.keys()):
                    try:
                        processor = running_streams[pid]
                        # 检查进程是否存活
                        if not processor.process:
                            continue
                            
                        process_status = processor.process.poll()
                        if process_status is not None:  # 进程已终止
                            # 检查是否是正常终止
                            if process_status != 0:
                                # 记录错误日志
                                error_log = processor.error_log if processor.error_log else "Unknown error"
                                log_action("PROCESS_ABNORMAL_TERMINATION", 
                                         pid=pid, 
                                         stream_id=processor.stream_id,
                                         error=error_log,
                                         exit_code=process_status)
                                
                                # 清理旧进程
                                processor.stop()
                                if pid in running_streams:
                                    del running_streams[pid]
                                    
                                # 自动重启流
                                new_processor = StreamProcessor(
                                    input_source=processor.input_source,
                                    stream_id=processor.stream_id,
                                    protocol=processor.protocol,
                                    buffered_mode=processor.buffered_mode
                                )
                                
                                # 添加重试机制
                                max_retries = 3
                                retry_count = 0
                                while retry_count < max_retries:
                                    new_pid, error = new_processor.start()
                                    if not error:
                                        running_streams[new_pid] = new_processor
                                        log_action("AUTO_RESTART_SUCCESS",
                                                input_source=processor.input_source,
                                                stream_id=processor.stream_id,
                                                protocol=processor.protocol,
                                                old_pid=pid,
                                                new_pid=new_pid,
                                                retry_count=retry_count + 1)
                                        break
                                    else:
                                        retry_count += 1
                                        log_action("AUTO_RESTART_RETRY",
                                                input_source=processor.input_source,
                                                stream_id=processor.stream_id,
                                                error=error,
                                                retry_count=retry_count)
                                        time.sleep(2)  # 等待2秒后重试
                                
                                if retry_count >= max_retries:
                                    log_action("AUTO_RESTART_FAILED",
                                            input_source=processor.input_source,
                                            stream_id=processor.stream_id,
                                            error="Max retries reached")
                    except Exception as e:
                        log_action("MONITOR_PROCESS_ERROR",
                                pid=pid,
                                error=str(e))
            except Exception as e:
                log_action("MONITOR_GLOBAL_ERROR",
                        error=str(e))

running_streams = {}
random.seed(int(datetime.now().timestamp()))

def generate_random_id(length=12):
    chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
    return ''.join(random.choices(chars, k=length))

def log_action(action, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} {action}"
    for k, v in kwargs.items():
        log_entry += f" | {k}: {v}"
    with open(HISTORY_FILE, "a") as f:
        f.write(log_entry + "\n")

def get_system_usage():
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        mem_usage = psutil.virtual_memory().percent
        return cpu_usage, mem_usage
    except Exception as e:
        print(f"Error getting system usage: {e}")
        return 0, 0

class AutoTask:    
    def __init__(self, processor, start_time, end_time):        
        self.processor = processor        
        # 确保时间是上海时区
        self.start_time = TIMEZONE.localize(start_time).replace(tzinfo=None)
        self.end_time = TIMEZONE.localize(end_time).replace(tzinfo=None)
        self.is_running = False
        self.stream_started = False
        self._stop_event = threading.Event()
        self.task_thread = None

    def _is_time_in_range(self, current_time):
        """Helper method to check if current time is within the scheduled range"""
        # Convert all times to minutes since midnight for easier comparison
        def to_minutes(dt):
            return dt.hour * 60 + dt.minute

        start_minutes = to_minutes(self.start_time)
        end_minutes = to_minutes(self.end_time)
        current_minutes = to_minutes(current_time)

        # Handle overnight schedules
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        else:
            return current_minutes >= start_minutes or current_minutes <= end_minutes

    def run_task(self):
        self.is_running = True
        logging.info(f"Starting scheduled task for stream {self.processor.stream_id}")
        logging.info(f"Schedule: {self.start_time.strftime('%H:%M')} to {self.end_time.strftime('%H:%M')} (Asia/Shanghai)")
        
        while not self._stop_event.is_set():
            try:
                now = get_shanghai_time()
                in_range = self._is_time_in_range(now)
                
                logging.debug(f"Stream {self.processor.stream_id} schedule check: "
                           f"current_time={now.strftime('%H:%M')} (Asia/Shanghai), "
                           f"in_range={in_range}, "
                           f"stream_started={self.stream_started}")
                
                if in_range:
                    if not self.stream_started:
                        logging.info(f"Starting scheduled stream {self.processor.stream_id}")
                        pid, error = self.processor.start()
                        if not error:
                            running_streams[pid] = self.processor
                            self.stream_started = True
                            log_action("AUTO_START_STREAM",
                                    input_source=self.processor.input_source,
                                    stream_id=self.processor.stream_id,
                                    protocol=self.processor.protocol)
                        else:
                            logging.error(f"Failed to start scheduled stream: {error}")
                else:
                    if self.stream_started:
                        logging.info(f"Stopping scheduled stream {self.processor.stream_id}")
                        self.processor.stop()
                        self.stream_started = False
                        log_action("AUTO_STOP_STREAM",
                                stream_id=self.processor.stream_id)
                
                if self._stop_event.wait(timeout=60):
                    break
            except Exception as e:
                logging.error(f"Error in scheduled task: {str(e)}")
                logging.error(traceback.format_exc())
                time.sleep(30)

        if self.stream_started:
            try:
                logging.info(f"Stopping stream {self.processor.stream_id} on task end")
                self.processor.stop()
                self.stream_started = False
                log_action("AUTO_STOP_STREAM",
                        stream_id=self.processor.stream_id,
                        reason="task_stopped")
            except Exception as e:
                logging.error(f"Error stopping stream on task end: {str(e)}")

        self.is_running = False
        logging.info(f"Scheduled task ended for stream {self.processor.stream_id}")

    def start(self):
        """Start the scheduled task"""
        if not self.is_running:
            self._stop_event.clear()
            # Create a new thread instance each time
            self.task_thread = threading.Thread(target=self.run_task)
            self.task_thread.daemon = True
            self.task_thread.start()
            log_action("SCHEDULE_START",
                    stream_id=self.processor.stream_id,
                    start_time=self.start_time.strftime("%H:%M"),
                    end_time=self.end_time.strftime("%H:%M"))

    def stop(self):
        """Stop the scheduled task"""
        if self.is_running:
            logging.info(f"Stopping scheduled task for stream {self.processor.stream_id}")
            self._stop_event.set()
            if self.task_thread and self.task_thread.is_alive():
                self.task_thread.join(timeout=5)  # Wait for thread to end
            self.is_running = False
            log_action("SCHEDULE_STOP",
                    stream_id=self.processor.stream_id)

@app.route("/", methods=["GET", "POST"])
def control_panel():
    try:
        if request.method == "POST":
            input_source = request.form.get("input_source", "").strip()
            stream_id = request.form.get("stream_id", "").strip()
            if not input_source:
                return jsonify({
                    "status": "error",
                    "message": "输入源不能为空"
                }), 400

            # 改进的协议检测逻辑
            detected_protocol = None
            input_parsed = urlparse(input_source)
            
            # 1. 检查URL协议头
            if input_parsed.scheme:
                if input_parsed.scheme.lower() == "hls":
                    detected_protocol = "hls"
                elif input_parsed.scheme.lower() == "udp":
                    detected_protocol = "udp"
                elif input_parsed.scheme.lower() == "rtp":
                    detected_protocol = "rtp"
                elif input_parsed.scheme.lower() == "rtsp":
                    detected_protocol = "rtsp"
                elif input_parsed.scheme.lower() == "rtmp":
                    detected_protocol = "rtmp"
                elif input_parsed.scheme.lower() in ["http", "https"]:
                    # 检查路径中的特殊前缀
                    if "/hls/" in input_parsed.path:
                        detected_protocol = "hls"
                    elif "/udp/" in input_parsed.path:
                        detected_protocol = "udp"
                    elif "/rtp/" in input_parsed.path:
                        detected_protocol = "rtp"
                    elif "/rtsp/" in input_parsed.path:
                        detected_protocol = "rtsp"
                    elif "/rtmp/" in input_parsed.path:
                        detected_protocol = "rtmp"
            
            # 2. 检查URL路径前缀匹配
            if not detected_protocol:
                for proto, info in SUPPORTED_PROTOCOLS.items():
                    if input_source.startswith(info["prefix"]):
                        detected_protocol = proto
                        break
            
            # 3. 默认使用HLS
            detected_protocol = detected_protocol or "hls"

            stream_id = stream_id or generate_random_id()
            stream_id = f"{stream_id}-{generate_random_id(6)}"

            processor = StreamProcessor(
                input_source=input_source,
                stream_id=stream_id,
                protocol=detected_protocol,
                buffered_mode=False
            )

            pid, error = processor.start()
            if error:
                logging.error(f"Failed to start stream: {error}")
                log_action("START_FAILED", input_source=input_source, error=error)
                return jsonify({
                    "status": "error",
                    "message": f"启动失败: {error}"
                }), 500

            running_streams[pid] = processor
            log_action("START_STREAM",
                    input_source=input_source,
                    stream_id=stream_id,
                    protocol=detected_protocol,
                    pid=pid)

            return jsonify({
                "status": "success",
                "stream_id": stream_id,
                "pid": pid
            })

        # GET 请求处理
        # 获取所有流信息（包括已停止的）
        streams = []
        for pid, proc in running_streams.items():
            try:
                is_running = proc.process and proc.process.poll() is None
                original_url = proc.input_source
                # Get schedule information
                schedule_info = None
                if proc.stream_id in running_tasks:
                    task = running_tasks[proc.stream_id]
                    if task.start_time and task.end_time:
                        try:
                            # Ensure we have proper datetime objects
                            start_time = task.start_time if isinstance(task.start_time, datetime) else datetime.strptime(task.start_time, '%H:%M')
                            end_time = task.end_time if isinstance(task.end_time, datetime) else datetime.strptime(task.end_time, '%H:%M')
                            schedule_info = {
                                'start_time': start_time.strftime('%H:%M'),
                                'end_time': end_time.strftime('%H:%M'),
                                'is_active': task.is_running
                            }
                        except (ValueError, TypeError) as e:
                            logging.error(f"Error formatting schedule times for stream {proc.stream_id}: {str(e)}")
                            schedule_info = None
                
                streams.append({
                    "pid": pid,
                    "stream_id": proc.stream_id,
                    "original_url": original_url,
                    "protocol": proc.protocol.upper(),
                    "rtmp_url": proc.rtmp_url,
                    "is_running": is_running,
                    "schedule": schedule_info
                })
            except Exception as e:
                logging.error(f"Error processing stream {pid}: {str(e)}")
                continue

        cpu_usage, mem_usage = get_system_usage()
        return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>直播推流控制面板</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            color: #333;
            background-color: #f5f5f5;
        }
        .container {
            background-color: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        .header {
            text-align: center;
            margin-bottom: 30px;
        }
        .header h1 {
            color: #3498db;
            margin: 0;
            font-size: 28px;
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: 600;
        }
        input[type="text"], textarea {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
            font-size: 14px;
            height: 38px;
        }
        textarea {
            resize: vertical;
        }
        button {
            padding: 8px 15px;
            margin-right: 10px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.2s;
        }
        button:hover {
            opacity: 0.9;
            transform: translateY(-1px);
        }
        .btn-start {
            background: #27ae60;
            color: white;
        }
        .btn-stop {
            background: #e74c3c;
            color: white;
        }
        .btn-copy {
            background: #3498db;
            color: white;
        }
        .btn-clear {
            background: #f39c12;
            color: white;
        }
        .btn-view {
            background: #9b59b6;
            color: white;
        }
        .process-list {
            margin: 20px 0;
        }
        .process-item {
            margin-bottom: 15px;
            padding: 15px;
            background: #f9f9f9;
            border-radius: 6px;
            border-left: 4px solid #3498db;
        }
        .url-box {
            margin: 10px 0;
            padding: 10px;
            background: #eaf2f8;
            border-radius: 4px;
            word-break: break-all;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .url-label {
            font-weight: bold;
            margin-bottom: 5px;
            color: #2980b9;
        }
        .url-content {
            flex-grow: 1;
        }
        .action-buttons {
            margin-top: 10px;
            display: flex;
            gap: 10px;
        }
        .status-badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            background: #2ecc71;
            color: white;
            margin-left: 10px;
        }
        .process-details {
            margin-top: 10px;
            padding: 10px;
            background: #f0f0f0;
            border-radius: 4px;
            display: none;
        }
        .toast {
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%);
            padding: 10px 20px;
            background-color: #e74c3c;
            color: white;
            border-radius: 4px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
            z-index: 1000;
            animation: fadeInOut 3s ease-in-out;
        }
        .status-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 5px;
        }
        .status-indicator.running {
            background-color: #2ecc71;
            animation: pulse-green 2s infinite;
        }
                .status-indicator.stopped {
                background-color: #e74c3c;
                }
                .schedule-box {
                display: inline-flex;
                align-items: center;
                gap: 10px;
                margin-right: 15px;
                }
                .delete-btn {
                background: #95a5a6;
                color: white;
                }
                .schedule-controls {
                display: flex;
                align-items: center;
                gap: 10px;
                }
                .time-input {
                padding: 5px;
                border:
                1px solid #ddd;
                border-radius: 4px;
                }
                .btn-schedule {
                background: #2ecc71;
                color: white;
                padding: 5px 10px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                }
                .btn-cancel-schedule {
                background: #e74c3c;
                color: white;
                padding: 5px 10px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                }
                .schedule-status {
                margin-top: 5px;
                font-size: 0.9em;
                color: #666;
                }
                @keyframes pulse-green {
                0% {
                box-shadow: 0 0 0 0 rgba(46, 204, 113, 0.7);
                }
                70% {
                box-shadow: 0 0 0 5px rgba(46, 204, 113, 0);
                }
                100% {
                box-shadow: 0 0 0 0 rgba(46, 204, 113, 0);
                }
                }
        .resource-usage {
            display: flex;
            justify-content: space-between;
            margin-bottom: 10px;
        }
        .resource-bar {
            flex-grow: 1;
            height: 20px;
            background-color: #f0f0f0;
            border-radius: 10px;
            margin: 0 10px;
            overflow: hidden;
        }
        .resource-fill {
            height: 100%;
            transition: width 0.5s ease;
        }
        .cpu-fill {
            background-color: #3498db;
        }
        .mem-fill {
            background-color: #9b59b6;
        }
        .checkbox-group {
            display: flex;
            align-items: center;
        }
        .checkbox-group input {
            margin-right: 5px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>直播推流控制面板</h1>
        </div>

        <form id="streamForm">
            <div class="form-group">
                <label for="input_source">输入源地址:</label>
                <input type="text" id="input_source" name="input_source" 
                    placeholder="支持格式: http://, rtsp://, rtmp://, rtp://, udp://" required>
            </div>

            <div class="form-group">
                <label for="stream_id">推流ID(可选):</label>
                <input type="text" id="stream_id" name="stream_id" 
                    placeholder="留空将自动生成随机ID">
            </div>

            <button type="submit" class="btn-start">开始推流</button>
        </form>

        <div class="process-list">
            <h2>推流进程列表 <span class="status-badge">{{ streams|length }} 个</span></h2>
            <div class="resource-usage">
                <span>CPU使用率:</span>
                <div class="resource-bar">
                    <div class="resource-fill cpu-fill" style="width: {{ cpu_usage }}%"></div>
                </div>
                <span>{{ cpu_usage }}%</span>
            </div>
            <div class="resource-usage">
                <span>内存使用率:</span>
                <div class="resource-bar">
                    <div class="resource-fill mem-fill" style="width: {{ mem_usage }}%"></div>
                </div>
                <span>{{ mem_usage }}%</span>
            </div>
            {% for stream in streams %}
                <div class="process-item">
                    <div class="url-box">
                        <div class="url-content">
                            <div class="url-label">
                                <span class="status-indicator {{ 'running' if stream.is_running else 'stopped' }}"></span>
                                推流地址:
                            </div>
                            https://ali.hlspull.yximgs.com/live/{{ stream.stream_id }}.flv
                        </div>
                        <button class="btn-copy" onclick="copyToClipboard('https://ali.hlspull.yximgs.com/live/{{ stream.stream_id|replace("'", "&#39;") }}.flv')">复制地址</button>
                        {% if stream.is_running %}
                        <form method="POST" action="/stop/{{ stream.pid }}" style="margin: 0;">
                            <button type="submit" class="btn-stop">停止进程</button>
                        </form>
                        {% else %}
                        <form method="POST" action="/start/{{ stream.pid }}" style="margin: 0;">
                            <button type="submit" class="btn-start">启动进程</button>
                        </form>
                        {% endif %}
                        <form method="POST" action="/delete/{{ stream.pid }}" style="margin: 0;">
                            <button type="submit" class="delete-btn">删除</button>
                        </form>
                    </div>
                    <div class="url-box">
                        <div class="schedule-box">
                            <input type="time" id="start_time_{{ stream.stream_id }}" class="time-input" 
                                   value="{{ stream.schedule.start_time if stream.schedule else '' }}">
                            <span>至</span>
                            <input type="time" id="end_time_{{ stream.stream_id }}" class="time-input"
                                   value="{{ stream.schedule.end_time if stream.schedule else '' }}">
                            <button onclick="scheduleStream('{{ stream.stream_id }}')" class="btn-schedule">设置定时</button>
                            {% if stream.schedule %}
                            <form method="POST" action="/cancel_schedule/{{ stream.stream_id }}" style="display: inline;">
                                <button type="submit" class="btn-cancel-schedule">取消定时</button>
                            </form>
                            <span class="schedule-status">
                                定时：{{ stream.schedule.start_time }} - {{ stream.schedule.end_time }}
                            </span>
                            {% endif %}
                        </div>
                        <div class="url-content">
                            <div class="url-label">输入源:</div>
                            {{ stream.original_url }}
                        </div>
                        <div class="url-label">协议: {{ stream.protocol }}</div>
                    </div>
                </div>
            {% else %}
                <p>当前没有推流进程</p>
            {% endfor %}
        </div>

        <div class="action-buttons">
            <a href="/history" class="btn-view">查看历史记录</a>
        </div>
    </div>

    <script>
        function copyToClipboard(text) {
            const input = document.createElement('textarea');
            input.value = text;
            document.body.appendChild(input);
            input.select();
            document.execCommand('copy');
            document.body.removeChild(input);
            showToast('已复制播放地址到剪贴板');
        }

        function showToast(message) {
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.textContent = message;
            document.body.appendChild(toast);

            setTimeout(() => {
                document.body.removeChild(toast);
            }, 3000);
        }

        async function scheduleStream(streamId) {
            const startTime = document.getElementById(`start_time_${streamId}`).value;
            const endTime = document.getElementById(`end_time_${streamId}`).value;

            if (!startTime || !endTime) {
                showToast('请设置开始和结束时间');
                return;
            }

            try {
                const formData = new FormData();
                formData.append('start_time', startTime);
                formData.append('end_time', endTime);

                const response = await fetch(`/schedule/${streamId}`, {
                    method: 'POST',
                    body: formData
                });

                const result = await response.json();
                if (result.status === 'success') {
                    window.location.reload();
                } else {
                    showToast(result.message || '设置定时失败');
                }
            } catch (error) {
                showToast('网络错误: ' + error.message);
            }
        }

        async function cancelSchedule(streamId) {
            try {
                const response = await fetch(`/cancel_schedule/${streamId}`, {
                    method: 'POST'
                });

                const result = await response.json();
                if (result.status === 'success') {
                    window.location.reload();
                } else {
                    showToast(result.message || '取消定时失败');
                }
            } catch (error) {
                showToast('网络错误: ' + error.message);
            }
        }

        document.getElementById('streamForm').addEventListener('submit', async function(e) {
            e.preventDefault();

            const input_source = document.getElementById('input_source').value.trim();
            const stream_id = document.getElementById('stream_id').value.trim();

            if (!input_source) {
                showToast('输入源不能为空');
                return;
            }

            try {
                const formData = new FormData();
                formData.append('input_source', input_source);
                formData.append('stream_id', stream_id);

                const response = await fetch('/', {
                    method: 'POST',
                    body: formData
                });

                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }

                const result = await response.json();
                if (result.status === 'success') {
                    window.location.reload();
                } else {
                    showToast(result.message || '操作失败');
                }
            } catch (error) {
                showToast('网络错误: ' + error.message);
            }
        });

        // 动态更新资源使用率
        function updateResourceUsage() {
            fetch('/resource-usage')
                .then(response => response.json())
                .then(data => {
                    const cpuUsage = data.cpu_usage;
                    const memUsage = data.mem_usage;

                    const cpuBar = document.querySelector('.cpu-fill');
                    const memBar = document.querySelector('.mem-fill');
                    const cpuText = document.querySelectorAll('.resource-usage')[0].querySelector('span:last-child');
                    const memText = document.querySelectorAll('.resource-usage')[1].querySelector('span:last-child');

                    cpuBar.style.width = `${cpuUsage}%`;
                    memBar.style.width = `${memUsage}%`;
                    cpuText.textContent = `${cpuUsage}%`;
                    memText.textContent = `${memUsage}%`;
                })
                .catch(error => console.error('Error fetching resource usage:', error));
        }

        // 每3秒更新一次资源使用率
        setInterval(updateResourceUsage, 3000);
    </script>
</body>
</html>
''', streams=streams, cpu_usage=cpu_usage, mem_usage=mem_usage)

    except Exception as e:
        logging.error(f"Error in control_panel: {str(e)}")
        logging.error(traceback.format_exc())
        return jsonify({
            "status": "error",
            "message": f"服务器内部错误: {str(e)}"
        }), 500

@app.route("/stop/<int:pid>", methods=["POST"])
def stop_stream(pid):
    if pid in running_streams:
        processor = running_streams[pid]
        
        # 先停止相关的定时任务
        if processor.stream_id in running_tasks:
            running_tasks[processor.stream_id].stop()
            del running_tasks[processor.stream_id]
            logging.info(f"Stopped scheduled task for stream {processor.stream_id}")

        # 确保停止所有相关进程
        try:
            # 停止主进程
            success = processor.stop()
            
            # 使用 psutil 确保所有子进程都被终止
            if processor.process and processor.process.pid:
                parent = psutil.Process(processor.process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                
                # 等待子进程终止
                gone, alive = psutil.wait_procs(children, timeout=3)
                
                # 如果还有进程存活，强制结束它们
                for p in alive:
                    try:
                        p.kill()
                    except psutil.NoSuchProcess:
                        pass

            log_action("STOP_STREAM", 
                      pid=pid, 
                      success=success, 
                      stream_id=processor.stream_id)
            
            return redirect(url_for('control_panel'))
        except Exception as e:
            logging.error(f"Error stopping stream: {str(e)}")
            return jsonify({
                "status": "error",
                "message": f"停止推流时出错: {str(e)}"
            }), 500
    return jsonify({
        "status": "error",
        "message": "推流任务不存在"
    })

@app.route("/start/<int:pid>", methods=["POST"])
def start_stream(pid):
    if pid in running_streams:
        processor = running_streams[pid]
        new_pid, error = processor.start()
        if error:
            return jsonify({
                "status": "error",
                "message": f"启动失败: {error}"
            }), 500
        
        # 更新PID
        running_streams[new_pid] = processor
        del running_streams[pid]
        
        log_action("START_STREAM",
                input_source=processor.input_source,
                stream_id=processor.stream_id,
                protocol=processor.protocol,
                pid=new_pid)
        return redirect(url_for('control_panel'))
    return jsonify({
        "status": "error",
        "message": "推流任务不存在"
    })

@app.route("/delete/<int:pid>", methods=["POST"])
def delete_stream(pid):
    if pid in running_streams:
        processor = running_streams[pid]
        # 如果进程还在运行，先停止它
        if processor.process and processor.process.poll() is None:
            processor.stop()
        # 如果有定时任务，取消它
        if processor.stream_id in running_tasks:
            running_tasks[processor.stream_id].stop()
            del running_tasks[processor.stream_id]
        # 删除进程记录
        del running_streams[pid]
        log_action("DELETE_STREAM", pid=pid, stream_id=processor.stream_id)
        return redirect(url_for('control_panel'))
    return jsonify({
        "status": "error",
        "message": "推流任务不存在"
    })

@app.route("/history")
def view_history():
    try:
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                for line in f:
                    parts = line.strip().split(" | ")
                    timestamp = parts[0].split(" ")[0]
                    action = parts[0].split(" ")[1]
                    entry = {
                        "timestamp": timestamp,
                        "action": action
                    }
                    for part in parts[1:]:
                        key, value = part.split(": ", 1)
                        entry[key] = value
                    history.append(entry)
        return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>推流历史记录</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            color: #333;
            background-color: #f5f5f5;
        }
        .container {
            background-color: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        .history-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        .history-table th, .history-table td {
            padding: 8px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        .history-table th {
            background-color: #f2f2f2;
        }
        .action-buttons {
            margin-top: 20px;
        }
        .btn-back {
            padding: 8px 15px;
            background: #3498db;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>推流历史记录</h2>
        <table class="history-table">
            <tr>
                <th>时间</th>
                <th>操作</th>
                <th>模式</th>
                <th>PID</th>
                <th>输入源</th>
                <th>流ID</th>
                <th>状态</th>
            </tr>
            {% for entry in history %}
                <tr>
                    <td>{{ entry.timestamp }}</td>
                    <td>{{ entry.action }}</td>
                    <td>{{ entry.buffered_mode if 'buffered_mode' in entry else '-' }}</td>
                    <td>{{ entry.pid if 'pid' in entry else '-' }}</td>
                    <td>{{ entry.input_source if 'input_source' in entry else '-' }}</td>
                    <td>{{ entry.stream_id if 'stream_id' in entry else '-' }}</td>
                    <td>{{ entry.success if 'success' in entry else '-' }}</td>
                </tr>
            {% endfor %}
        </table>
        <div class="action-buttons">
            <a href="/" class="btn-back">返回控制面板</a>
        </div>
    </div>
</body>
</html>
''', history=history)
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"读取历史记录失败: {str(e)}"
        })

@app.route("/restart/<int:pid>")
def restart_stream(pid):
    for proc in running_streams.values():
        if proc.process and proc.process.pid == pid:
            input_source = proc.input_source
            stream_id = proc.stream_id
            protocol = proc.protocol
            buffered_mode = proc.buffered_mode

            stop_stream(pid)

            new_processor = StreamProcessor(
                input_source=input_source,
                stream_id=stream_id,
                protocol=protocol,
                buffered_mode=buffered_mode
            )
            new_pid, error = new_processor.start()
            if error:
                return jsonify({
                    "status": "error",
                    "message": f"重启失败: {error}"
                })

            running_streams[new_pid] = new_processor
            log_action("RESTART_STREAM",
                input_source=input_source,
                stream_id=stream_id,
                protocol=protocol,
                buffered_mode="buffered" if buffered_mode else "direct",
                pid=new_pid)
            return jsonify({
                "status": "success",
                "message": "推流已重启",
                "new_pid": new_pid
            })

    return jsonify({
        "status": "error",
        "message": "未找到指定PID的推流进程"
    })

@app.route("/resource-usage")
def get_resource_usage():
    cpu_usage, mem_usage = get_system_usage()
    return jsonify({
        'cpu_usage': cpu_usage,
        'mem_usage': mem_usage
    })

@app.route("/schedule/<stream_id>", methods=["POST"])
def schedule_stream(stream_id):
    """Set up a scheduled task for a stream"""
    try:
        start_time_str = request.form.get("start_time")
        end_time_str = request.form.get("end_time")
        
        if not start_time_str or not end_time_str:
            return jsonify({
                "status": "error",
                "message": "Start and end times are required"
            }), 400

        # Validate time format and convert to Shanghai time
        try:
            # Parse the input times
            start_time = datetime.strptime(start_time_str, "%H:%M")
            end_time = datetime.strptime(end_time_str, "%H:%M")
            
            # Set to today's date in Shanghai time
            today = get_shanghai_time().date()
            start_time = datetime.combine(today, start_time.time())
            end_time = datetime.combine(today, end_time.time())
            
            # If end time is earlier than start time, assume it's for the next day
            if end_time <= start_time:
                end_time += timedelta(days=1)
                
        except ValueError:
            return jsonify({
                "status": "error",
                "message": "Invalid time format. Please use HH:MM format (24-hour)"
            }), 400

        # Find the corresponding processor
        processor = None
        for proc in running_streams.values():
            if proc.stream_id == stream_id:
                processor = proc
                break

        if not processor:
            return jsonify({
                "status": "error",
                "message": "Stream not found"
            }), 404

        # Stop existing scheduled task if any
        if stream_id in running_tasks:
            running_tasks[stream_id].stop()

        # Create and start new scheduled task
        auto_task = AutoTask(processor, start_time, end_time)
        auto_task.start()
        running_tasks[stream_id] = auto_task

        log_action("SET_SCHEDULE",
                stream_id=stream_id,
                start_time=start_time_str,
                end_time=end_time_str,
                timezone="Asia/Shanghai")

        return jsonify({
            "status": "success",
            "message": f"Stream scheduled to run daily from {start_time_str} to {end_time_str} (Asia/Shanghai)",
            "schedule": {
                "start_time": start_time_str,
                "end_time": end_time_str,
                "is_active": True,
                "timezone": "Asia/Shanghai"
            }
        })
    except Exception as e:
        logging.error(f"Failed to set schedule: {str(e)}")
        logging.error(traceback.format_exc())
        return jsonify({
            "status": "error",
            "message": f"Failed to set schedule: {str(e)}"
        }), 500

@app.route("/cancel_schedule/<stream_id>", methods=["POST"])
def cancel_schedule(stream_id):
    """Cancel a scheduled task"""
    try:
        if stream_id in running_tasks:
            running_tasks[stream_id].stop()
            del running_tasks[stream_id]
            return jsonify({
                "status": "success",
                "message": "已取消定时任务"
            })
        return jsonify({
            "status": "error",
            "message": "未找到对应的定时任务"
        }), 404
    except Exception as e:
        logging.error(f"Failed to cancel schedule: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"取消定时失败: {str(e)}"
        }), 500

if __name__ == "__main__":
    try:
        if not os.path.exists(HISTORY_FILE):
            open(HISTORY_FILE, "w").close()
        
        # 启动监控线程
        monitor_thread = Thread(target=monitor_streams, daemon=True)
        monitor_thread.start()
        
        app.run(host="0.0.0.0", port=36336, debug=False)
    except Exception as e:
        logging.error(f"Server startup error: {str(e)}")
        logging.error(traceback.format_exc())