## 🚀 快速开始

使用以下命令克隆项目、构建镜像并运行容器(使用alpine系统镜像可以最小)：

```bash
git clone https://github.com/iii80/ffmpeg-streaming.git && cd ffmpeg-streaming
docker build -t ffmpeg-streaming .
docker run -d -p 36336:36336 --name ffmpeg-streaming ffmpeg-streaming
