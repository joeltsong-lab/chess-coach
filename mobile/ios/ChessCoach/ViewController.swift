import UIKit
import WebKit

/// 主界面：全屏 WKWebView 加载电脑上的 /mobile 页面。
///
/// - 首次启动（没存过地址）→ present ConfigViewController
/// - 之后启动 → 直接加载已保存的地址
/// - 加载失败 → 显示 overlay（重新连接 / 修改电脑地址）
/// - 页面脚本执行前注入 JS，给所有 /api/mobile/* 请求自动加 X-Mobile-Token 头
class ViewController: UIViewController {

    @IBOutlet weak var webView: WKWebView!
    @IBOutlet weak var overlayView: UIView!
    @IBOutlet weak var overlayDetailLabel: UILabel!
    @IBOutlet weak var retryButton: UIButton!
    @IBOutlet weak var settingsButton: UIButton!

    private var configPresented = false

    override func viewDidLoad() {
        super.viewDidLoad()

        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = true

        overlayView.isHidden = true
        overlayView.backgroundColor = UIColor(red: 0.10, green: 0.10, blue: 0.18, alpha: 1.0)
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if AppConfig.isConfigured {
            if webView.url == nil { loadPage() }
        } else if !configPresented {
            presentConfig()
        }
    }

    // MARK: - 加载

    private func loadPage() {
        guard let url = AppConfig.pageURL() else {
            presentConfig()
            return
        }
        overlayView.isHidden = true
        installTokenScript()
        webView.load(URLRequest(url: url))
    }

    /// 注入拦截脚本：包装 fetch / XMLHttpRequest，给 /api/mobile/* 补上 Token 头
    private func installTokenScript() {
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()

        let token = AppConfig.effectiveToken
        guard !token.isEmpty else { return }

        let literal = Self.jsStringLiteral(token)
        let source = Self.tokenScriptTemplate.replacingOccurrences(of: "__TOKEN__", with: literal)
        let script = WKUserScript(source: source,
                                  injectionTime: .atDocumentStart,
                                  forMainFrameOnly: false)
        controller.addUserScript(script)
    }

    // MARK: - 交互

    @IBAction func retryTapped(_ sender: UIButton) {
        if AppConfig.isConfigured {
            loadPage()
        } else {
            presentConfig()
        }
    }

    @IBAction func settingsTapped(_ sender: UIButton) {
        presentConfig()
    }

    private func presentConfig() {
        guard !configPresented else { return }
        configPresented = true

        let config = ConfigViewController()
        config.onFinish = { [weak self] saved in
            guard let self = self else { return }
            self.configPresented = false
            if saved {
                self.loadPage()
            } else if AppConfig.isConfigured {
                self.overlayView.isHidden = true
            } else {
                self.showOverlay(detail: "尚未设置电脑地址，点「重新连接」填写。")
            }
        }

        let nav = UINavigationController(rootViewController: config)
        present(nav, animated: true)
    }

    private func showOverlay(detail: String) {
        overlayDetailLabel.text = detail
        overlayView.isHidden = false
    }

    // MARK: - 工具

    /// 把任意字符串安全地嵌进 JS 字符串字面量（JSON 编码后去掉外层方括号）
    static func jsStringLiteral(_ value: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              let json = String(data: data, encoding: .utf8),
              json.count >= 2 else {
            return "\"\""
        }
        return String(json.dropFirst().dropLast())
    }

    /// 注入脚本模板；__TOKEN__ 会被替换成 JSON 字符串字面量
    static let tokenScriptTemplate = """
        (function () {
          var TOKEN = __TOKEN__;
          if (!TOKEN) return;

          function toUrl(u) {
            try { return new URL(u, location.href); } catch (e) { return null; }
          }
          function isApi(u) {
            return !!u && u.pathname.indexOf('/api/mobile/') === 0;
          }

          // 1) fetch
          var nativeFetch = window.fetch;
          if (nativeFetch) {
            window.fetch = function (input, init) {
              try {
                init = init || {};
                var url = (typeof input === 'string') ? input : (input && input.url);
                if (isApi(toUrl(url))) {
                  var h = new Headers(init.headers || (input && input.headers) || {});
                  h.set('X-Mobile-Token', TOKEN);
                  init.headers = h;
                }
              } catch (e) {}
              return nativeFetch.call(this, input, init);
            };
          }

          // 2) XMLHttpRequest
          var nativeOpen = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function (method, url) {
            this.__ccUrl = url;
            return nativeOpen.apply(this, arguments);
          };
          var nativeSend = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.send = function (body) {
            try {
              if (isApi(toUrl(this.__ccUrl))) {
                this.setRequestHeader('X-Mobile-Token', TOKEN);
              }
            } catch (e) {}
            return nativeSend.apply(this, arguments);
          };
        })();
        """
}

// MARK: - WKNavigationDelegate

extension ViewController: WKNavigationDelegate {

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        overlayView.isHidden = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showOverlay(detail: errorMessage(error))
    }

    func webView(_ webView: WKWebView,
                 didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        showOverlay(detail: errorMessage(error))
    }

    private func errorMessage(_ error: Error) -> String {
        let ns = error as NSError
        // -1009 = 无网络连接
        let hint = (ns.code == NSURLErrorNotConnectedToInternet)
            ? "手机当前没有网络。"
            : "请确认：电脑上的服务正在运行，手机与电脑连的是同一个 Wi-Fi，设置里的 IP 和端口正确。"
        return "\(error.localizedDescription)\n\n\(hint)"
    }
}
