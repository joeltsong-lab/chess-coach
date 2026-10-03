import Foundation

/// 电脑地址 / 端口 / Token 的本地存储（UserDefaults）。
/// 首次启动时由 ConfigViewController 写入，之后 ViewController 直接读取。
enum AppConfig {

    /// 默认端口与 scripts/run.sh 里的 mobile 实例一致
    static let defaultPort = 5002

    private static let kHost = "cc_host"
    private static let kPort = "cc_port"
    private static let kToken = "cc_token"
    private static let kTokenEnabled = "cc_token_enabled"

    static var host: String {
        get { (UserDefaults.standard.string(forKey: kHost) ?? "").trimmingCharacters(in: .whitespaces) }
        set { UserDefaults.standard.set(newValue.trimmingCharacters(in: .whitespaces), forKey: kHost) }
    }

    static var port: Int {
        get {
            let value = UserDefaults.standard.integer(forKey: kPort)
            return value == 0 ? defaultPort : value
        }
        set { UserDefaults.standard.set(newValue, forKey: kPort) }
    }

    static var token: String {
        get { (UserDefaults.standard.string(forKey: kToken) ?? "").trimmingCharacters(in: .whitespaces) }
        set { UserDefaults.standard.set(newValue.trimmingCharacters(in: .whitespaces), forKey: kToken) }
    }

    static var tokenEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: kTokenEnabled) }
        set { UserDefaults.standard.set(newValue, forKey: kTokenEnabled) }
    }

    /// 是否已经配置过电脑地址
    static var isConfigured: Bool { !host.isEmpty }

    /// 实际要发的 Token：开关关掉时返回空串
    static var effectiveToken: String { tokenEnabled ? token : "" }

    /// 手机版页面地址，如 http://192.168.1.100:5002/mobile
    static func pageURL() -> URL? {
        URL(string: "http://\(host):\(port)/mobile")
    }

    /// 状态接口地址，「测试连接」用
    static func statusURL(host: String, port: Int) -> URL? {
        URL(string: "http://\(host):\(port)/api/mobile/status")
    }
}
