# 🎵 灵动音乐盒 (Music Island)

一款基于 PyQt6 的桌面音乐播放器，采用 Fluent Design 风格，支持多平台在线搜索和本地音乐播放，配有类似 Apple Dynamic Island 的悬浮灵动岛歌词控件。

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![PyQt6](https://img.shields.io/badge/PyQt6-6.5+-green.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## ✨ 功能特性

- 🎶 **[New]网易云音乐歌单&专辑导入** - 通过第三方 API 导入网易云音乐的歌单&专辑，输入ID即可导入
- 📄 **[New]播放列表导出&导入** - 将播放列表保存为.json格式的文件，程序每次启动就会自动导入程序目录下的 `list` 文件夹内的.json列表，程序内支持重命名等操作
- 🎵 **网易云音乐搜索** — 通过第三方 API 搜索并播放网易云音乐，支持默认/备用两种 API 源
- 📁 **本地音乐播放** — 支持 mp3 / flac / ogg / m4a / aac / wav / wma 格式，自动读取内嵌元数据（歌名、歌手、专辑、歌词、封面）
- 🏝️ **灵动岛悬浮窗** — 屏幕顶部居中的迷你播放控件，展开后显示歌词、进度条、播放控制
- 📝 **歌词同步** — LRC 格式歌词实时逐行高亮显示
- 🖼️ **封面自动加载** — 在线歌曲自动下载封面，本地歌曲自动匹配同名图片
- 📋 **播放列表** — 支持添加、移除、清空，双击播放歌曲
- 🔁 **播放模式** — 单曲循环 / 列表循环 / 随机播放
- 🖥️ **系统托盘** — 关闭主窗口后灵动岛仍保留，托盘可快速显示窗口、播放/暂停、退出

## ⚠️ 特此说明

**尊重音乐平台版权，尊重正版！** 本软件仅供学习交流使用，请勿用于商业用途。

## 📁 项目结构

```
Music-Island/
├── list/            # 歌单&专辑存放文件夹
├── main.py          # 全部源码（单文件）
├── requirements.txt # Python 依赖
├── LICENSE          # MIT 协议
└── README.md        # 项目说明
```

## 🚀 安装与运行

```bash
# 克隆仓库
git clone https://github.com/daileyu1221/Music-Island.git
cd Music-Island

# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```
> [!TIP]如果qfluentwidgets安装失败，可以尝试 pip install PyQt6-Fluent-Widgets

## 📦 主要依赖

| 库 | 用途 |
|---|---|
| PyQt6 | GUI 框架（Core / Widgets / Gui / Multimedia） |
| qfluentwidgets | Fluent Design 风格组件 |
| requests | HTTP 请求（API 调用、封面下载） |
| mutagen | 本地音频文件元数据读取（ID3 / MP4 / FLAC / Ogg） |

## 📖 使用说明

1. 启动后自动打开主窗口和灵动岛悬浮窗
2. 在侧边栏选择「网易云音乐搜索」，输入歌曲名搜索
3. 双击搜索结果即可播放，歌曲自动添加到播放列表
4. 右键歌曲可「播放」或「添加到播放列表」
5. 鼠标悬停灵动岛可展开查看歌词和控制播放
6. 主窗口可关闭，灵动岛和托盘仍保留
7. 本地音乐页面支持添加音频文件或整个文件夹，自动匹配同名歌词（.lrc）和封面图片
8. 侧边栏选择「歌单/专辑导入」，可以使用歌单&专辑ID进行导入操作

## 👨‍💻 作者

文宇香香（文宇香香工作室）

## 📄 License

本项目采用 [MIT License](LICENSE) 开源协议。

> **Disclaimer**: This project is for educational purposes only. All music copyrights belong to their respective owners. "Dynamic Island" is a trademark of Apple Inc. This project is not affiliated with or endorsed by Apple Inc.

[文宇香香工作室](https://wyxxgzs.pages.dev)
