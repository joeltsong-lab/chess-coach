# 手机端：安卓 / iOS WebView 壳 App

把电脑上跑的手机 Web 端（`/mobile` 页面）包装成原生 App：全屏、无浏览器 UI、可当 App 用。

**纯远程访问**：App 本身不含引擎，所有分析都由电脑上的 Flask 服务完成，因此电脑必须开着服务、
手机与电脑在同一 Wi-Fi 下。

## 目录结构

```
mobile/
├── android/                              # Android 工程（Kotlin）
│   ├── app/src/main/java/com/chesscoach/mobile/
│   │   ├── MainActivity.kt               # 全屏 WebView + 返回键 + Token 注入
│   │   ├── ConfigActivity.kt             # 电脑 IP / 端口 / Token 设置 + 测试连接
│   │   └── Prefs.kt                      # SharedPreferences 存储
│   ├── app/src/main/res/                 # 布局 / 字符串 / 主题 / network_security_config
│   ├── app/src/main/AndroidManifest.xml
│   ├── app/build.gradle.kts
│   ├── build.gradle.kts / settings.gradle.kts / gradle.properties
│   └── gradle/wrapper/gradle-wrapper.properties
└── ios/                                  # Xcode 工程（Swift）
    ├── ChessCoach.xcodeproj/
    └── ChessCoach/
        ├── AppDelegate.swift
        ├── SceneDelegate.swift
        ├── ViewController.swift          # 全屏 WKWebView + 失败重连 overlay
        ├── ConfigViewController.swift    # 连接设置（代码构建 UI）
        ├── AppConfig.swift               # UserDefaults 存储
        ├── Info.plist
        ├── Base.lproj/Main.storyboard
        └── Assets.xcassets
```

## 前置条件

1. 电脑上启动 Flask 服务：`scripts/run.sh mobile`（监听 `0.0.0.0:5002`）
2. 手机与电脑连**同一个 Wi-Fi**
3. 查电脑局域网 IP：Windows `ipconfig`，macOS/Linux `ifconfig` / `ip addr`

## 安卓：构建

1. Android Studio → **Open** → 选择 `mobile/android` 目录
2. 首次 Sync 会自动下载依赖。仓库未提交 Gradle wrapper 的二进制 jar，
   若 IDE 提示缺少 wrapper，按提示生成（或运行 `gradle wrapper --gradle-version 8.7`）即可
3. **Build → Build Bundle(s) / APK(s) → Build APK(s)**
4. 产物在 `app/build/outputs/apk/debug/app-debug.apk`，传到手机安装
   （需在手机上允许「安装未知来源应用」）

环境要求：minSdk 24 / targetSdk 34 / JDK 17。

## iOS：构建

1. Xcode → **Open** → `mobile/ios/ChessCoach.xcodeproj`
2. 选 target **ChessCoach** → **Signing & Capabilities** → 选自己的 Team（个人 Apple ID 即可）
3. 连接 iPhone（或选模拟器）→ 点 **Run**
4. 真机首次运行需信任证书：手机 设置 → 通用 → VPN 与设备管理 → 信任该开发者

环境要求：iOS 14.0+ / Swift 5。AppIcon 资源位是空的，可自行拖入 1024×1024 图标。

## 首次启动配置

App 第一次打开会进入设置页：

| 项 | 说明 |
| --- | --- |
| 电脑 IP 地址 | 如 `192.168.1.100` |
| 端口 | 默认 **5002**（与 `scripts/run.sh mobile` 一致） |
| 访问 Token | 可选；服务端设了 `MOBILE_TOKEN` 才需要，打开开关后填写 |

- **测试连接**：请求 `/api/mobile/status`，成功显示引擎 / LLM 就绪状态，失败给出原因
  （安卓显示在设置页底部，iOS 用红/绿点 + 文字）
- **保存**：写入本地存储并进入全屏棋盘

之后每次启动直接加载已保存的地址，不再进设置页。

## Token 自动注入

两个平台都在**页面脚本执行前**注入 JS，包装 `fetch` 与 `XMLHttpRequest`：
凡是打到 `/api/mobile/*` 的请求都自动补上 `X-Mobile-Token` 头，
与服务端 `mobile_routes.py` 的校验方式一致。

- Android：`WebViewCompat.addDocumentStartJavaScript`（老版本 WebView 退回 `onPageStarted` 注入）
- iOS：`WKUserScript(injectionTime: .atDocumentStart)`

页面设置里也有一份 Token，两者取值一致，不冲突。

## 加载失败

- **Android**：弹对话框「重试 / 修改设置」
- **iOS**：全屏 overlay「重新连接 / 修改电脑地址」

两端都会提示检查：服务是否在跑、是否同一 Wi-Fi、IP 与端口是否正确。

## 常见问题

- **打不开**：确认服务已启动、手机与电脑同一 Wi-Fi、IP/端口正确；
  Windows 防火墙需放行 `python.exe`
- **iOS 首次连接弹「是否允许访问本地网络」**：必须点允许，否则请求会被系统拦截
  （`NSLocalNetworkUsageDescription` 已配置）
- **端口别填错**：`5000` 是 main 分支的 stable 实例，手机版跑在 `5002`

## 本地端口

预留 5002（`scripts/run.sh mobile`）。

## 起步

```bash
git merge main                        # 把 main 的最新改动并进当前分支
scripts/branch_manager.sh sync feature/mobile   # 同上，脚本会先检查工作区是否干净
```
