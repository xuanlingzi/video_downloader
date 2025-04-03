#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Video Downloader API
~~~~~~~~~~~~~~~~~~

一个基于 Flask 的视频下载 API 服务，支持从多个平台下载视频和音频。

主要功能:
- 支持视频和音频下载
- 自动文件清理
- 文件名规范化
- 定时任务调度
- 支持自定义CA证书

使用方法:
    $ python app.py

环境变量:
    DOWNLOAD_DIR: 下载目录路径（可选，默认使用临时目录）
    PORT: 服务端口（可选，默认 8000）
    CLEANUP_INTERVAL_HOURS: 文件清理间隔（可选，默认 24 小时）
    CA_CERT_PATH: CA证书路径（可选，用于处理MITM代理）
"""

import os
import re
import glob
import hashlib
import logging
import tempfile
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from urllib.parse import quote

from flask import Flask, request, jsonify, send_file, Response
import yt_dlp
from apscheduler.schedulers.background import BackgroundScheduler

# 配置常量
DEFAULT_PORT = 8000
DEFAULT_CLEANUP_INTERVAL_HOURS = 24
DEFAULT_MAX_FILENAME_LENGTH = 100

# 环境变量配置
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', os.path.join(tempfile.gettempdir(), 'video_downloader'))
PORT = int(os.getenv('PORT', DEFAULT_PORT))
CLEANUP_INTERVAL_HOURS = int(os.getenv('CLEANUP_INTERVAL_HOURS', DEFAULT_CLEANUP_INTERVAL_HOURS))
CA_CERT_PATH = os.getenv('CA_CERT_PATH')

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 初始化 Flask 应用
app = Flask(__name__)

class DownloadError(Exception):
    """下载过程中的自定义异常"""
    pass

def ensure_download_dir() -> None:
    """确保下载目录存在"""
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
        logger.info(f"创建下载目录: {DOWNLOAD_DIR}")

def sanitize_filename(filename: str, max_length: int = DEFAULT_MAX_FILENAME_LENGTH) -> str:
    """
    清理并规范化文件名。

    Args:
        filename: 原始文件名
        max_length: 最大文件名长度

    Returns:
        str: 处理后的文件名
    """
    # 移除不安全的字符
    filename = re.sub(r'[^\w\s-]', '', filename)
    # 将空格替换为下划线
    filename = re.sub(r'\s+', '_', filename)
    # 限制文件名长度
    if len(filename) > max_length:
        name, ext = os.path.splitext(filename)
        filename = name[:max_length-len(ext)] + ext
    return filename

def cleanup_downloads() -> None:
    """清理过期的下载文件"""
    try:
        logger.info("开始清理下载目录...")
        now = datetime.now()
        count = 0
        
        for item in os.listdir(DOWNLOAD_DIR):
            item_path = os.path.join(DOWNLOAD_DIR, item)
            if not os.path.isfile(item_path):
                continue
                
            file_mtime = datetime.fromtimestamp(os.path.getmtime(item_path))
            if now - file_mtime > timedelta(hours=CLEANUP_INTERVAL_HOURS):
                try:
                    os.remove(item_path)
                    count += 1
                except OSError as e:
                    logger.error(f"删除文件失败 {item_path}: {e}")
        
        logger.info(f"清理完成，共删除 {count} 个文件")
    except Exception as e:
        logger.error(f"清理过程中发生错误: {e}")

def get_download_options(format_type: str, base_filename: str) -> Dict[str, Any]:
    """
    获取下载选项配置。

    Args:
        format_type: 下载格式类型 ('video' 或 'audio')
        base_filename: 基础文件名

    Returns:
        Dict[str, Any]: yt-dlp 配置选项
    """
    format_opt = 'best' if format_type == 'video' else 'bestaudio'
    
    options = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, base_filename + '.%(ext)s'),
        'format': format_opt,
        'progress_hooks': [lambda d: logger.info(f'下载进度: {d.get("status")}, {d.get("filename", "未知文件名")}')],
    }
    
    # 如果指定了CA证书路径，添加到选项中
    if CA_CERT_PATH and os.path.exists(CA_CERT_PATH):
        options['nocheckcertificate'] = False
        options['cafile'] = CA_CERT_PATH
        logger.info(f"使用自定义CA证书: {CA_CERT_PATH}")
    else:
        options['nocheckcertificate'] = True
        logger.warning("未指定CA证书或证书文件不存在，将跳过证书验证")
    
    if format_type == 'audio':
        options.update({
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
            }]
        })
    
    return options

@app.route('/download', methods=['GET'])
def download_video() -> Response:
    """
    处理视频下载请求。

    Query Parameters:
        url (required): 要下载的视频URL
        format (optional): 下载格式 ('video' 或 'audio'，默认为 'video')

    Returns:
        Response: 下载的文件或错误信息
    """
    url = request.args.get('url')
    format_type = request.args.get('format', 'video')
    
    if not url:
        return jsonify({'error': 'URL parameter is required'}), 400
    
    if format_type not in ('video', 'audio'):
        return jsonify({'error': 'Invalid format type'}), 400

    try:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        base_filename = f'video_{url_hash}'
        
        ydl_opts = get_download_options(format_type, base_filename)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_files = glob.glob(os.path.join(DOWNLOAD_DIR, base_filename + '.*'))
            
            if not downloaded_files:
                raise DownloadError('下载完成但未找到文件')
                
            downloaded_file = downloaded_files[0]
            if not os.path.exists(downloaded_file):
                raise DownloadError(f'下载文件未找到: {downloaded_file}')
            
            original_title = info.get('title', 'video')
            file_ext = os.path.splitext(downloaded_file)[1]
            safe_title = sanitize_filename(original_title)
            
            # 对文件名进行URL编码
            encoded_filename = quote(f"{safe_title}{file_ext}")
            
            response = send_file(
                downloaded_file,
                as_attachment=True,
                download_name=encoded_filename,
                mimetype='application/octet-stream'
            )
            
            # 设置Content-Disposition头，使用RFC 5987编码
            response.headers.update({
                'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}",
                'X-Filename': encoded_filename,
                'X-File-Type': format_type
            })
            
            return response
            
    except DownloadError as e:
        logger.error(f"下载错误: {e}")
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f"未预期的错误: {e}")
        return jsonify({'error': '服务器内部错误'}), 500

def init_scheduler() -> None:
    """初始化定时任务调度器"""
    scheduler = BackgroundScheduler()
    scheduler.add_job(cleanup_downloads, 'cron', hour=2, minute=0)
    scheduler.start()
    logger.info("定时清理任务已启动")

def main() -> None:
    """应用程序入口点"""
    try:
        ensure_download_dir()
        init_scheduler()
        logger.info(f'服务器将在端口 {PORT} 上启动')
        app.run(host='0.0.0.0', port=PORT)
    except Exception as e:
        logger.error(f'启动服务器时发生错误: {e}')
        raise

if __name__ == '__main__':
    main() 