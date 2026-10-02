把 Pikafish 引擎文件放在这个目录里。

需要两个文件:

  pikafish.exe   Windows 引擎可执行文件 (macOS / Linux 下命名为 pikafish, 并 chmod +x)
  pikafish.nnue  NNUE 权重文件

下载方法:

  1) 打开 https://github.com/official-pikafish/Pikafish/releases
  2) 下载最新 release 的 Assets 里的 Pikafish.<日期>.7z
     该包是通用二进制包, 内含 Windows / Linux / macOS / RISC-V 等各平台版本
  3) 用 7-Zip 解压, 找到 windows 目录下的可执行文件, 复制到本目录并重命名为 pikafish.exe
  4) 把随包提供的 pikafish.nnue 也复制到本目录

也可以不放在这里, 改用环境变量指定路径:

  PIKAFISH_PATH  引擎可执行文件路径
  PIKAFISH_NNUE  权重文件路径

或者运行 demo.py 时用参数指定:

  python demo.py --engine D:\path\to\pikafish.exe --nnue D:\path\to\pikafish.nnue
