git clone https://github.com/iii80/ffmpeg-streaming.git && cd ffmpeg-streaming
docker build -t ffmpeg-streaming .
docker run -d -p 36336:36336 --name ffmpeg-streaming ffmpeg-streaming
