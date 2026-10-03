import UIKit

/// 连接设置：电脑 IP / 端口 / Token。
///
/// - 「测试连接」用 URLSession 打 /api/mobile/status，红/绿点 + 文字反馈
/// - 「保存」写入 UserDefaults 后 dismiss，由 ViewController 重新加载页面
/// - 界面用代码构建（不放进 storyboard，避免和 Main.storyboard 的布局耦合）
final class ConfigViewController: UIViewController {

    /// 结束回调：true = 已保存，false = 取消
    var onFinish: ((Bool) -> Void)?

    private let hostField = UITextField()
    private let portField = UITextField()
    private let tokenField = UITextField()
    private let tokenSwitch = UISwitch()
    private let testButton = UIButton(type: .system)
    private let statusDot = UIView()
    private let statusLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()

        title = "连接电脑"
        view.backgroundColor = UIColor(red: 0.96, green: 0.95, blue: 0.93, alpha: 1.0)
        isModalInPresentation = true

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "取消", style: .plain, target: self, action: #selector(cancelTapped))
        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "保存", style: .done, target: self, action: #selector(saveTapped))

        buildUI()
        fillValues()
    }

    // MARK: - UI

    private func buildUI() {
        let scroll = UIScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.keyboardDismissMode = .interactive
        view.addSubview(scroll)

        let stack = UIStackView()
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.axis = .vertical
        stack.spacing = 16
        scroll.addSubview(stack)

        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            stack.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor, constant: -24),
            stack.leadingAnchor.constraint(equalTo: scroll.frameLayoutGuide.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: scroll.frameLayoutGuide.trailingAnchor, constant: -20),
        ])

        let intro = UILabel()
        intro.text = "电脑上先运行 Flask 服务（scripts/run.sh mobile），手机与电脑连同一个 Wi-Fi。"
        intro.numberOfLines = 0
        intro.font = .systemFont(ofSize: 13)
        intro.textColor = .secondaryLabel

        // 状态：圆点 + 文字
        statusLabel.numberOfLines = 0
        statusLabel.font = .systemFont(ofSize: 13)
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        statusDot.layer.cornerRadius = 5
        statusDot.backgroundColor = .systemGray3
        NSLayoutConstraint.activate([
            statusDot.widthAnchor.constraint(equalToConstant: 10),
            statusDot.heightAnchor.constraint(equalToConstant: 10),
        ])
        let statusRow = UIStackView(arrangedSubviews: [statusDot, statusLabel])
        statusRow.axis = .horizontal
        statusRow.spacing = 8
        statusRow.alignment = .top

        testButton.setTitle("测试连接", for: .normal)
        testButton.titleLabel?.font = .systemFont(ofSize: 16, weight: .medium)
        testButton.layer.borderWidth = 1
        testButton.layer.borderColor = UIColor.systemBlue.cgColor
        testButton.layer.cornerRadius = 10
        testButton.heightAnchor.constraint(equalToConstant: 44).isActive = true
        testButton.addTarget(self, action: #selector(testTapped), for: .touchUpInside)

        tokenSwitch.addTarget(self, action: #selector(tokenSwitchChanged), for: .valueChanged)
        let switchRow = UIStackView(arrangedSubviews: [makeCaption("启用 Token"), tokenSwitch])
        switchRow.axis = .horizontal
        switchRow.alignment = .center
        switchRow.distribution = .equalSpacing

        stack.addArrangedSubview(intro)
        stack.addArrangedSubview(makeField("电脑 IP 地址", hostField,
                                           placeholder: "192.168.1.100", keyboard: .URL))
        stack.addArrangedSubview(makeField("端口", portField,
                                           placeholder: "\(AppConfig.defaultPort)", keyboard: .numberPad))
        stack.addArrangedSubview(switchRow)
        stack.addArrangedSubview(makeField("访问 Token（可选）", tokenField,
                                           placeholder: "服务端设了 MOBILE_TOKEN 才需要", keyboard: .default))
        stack.addArrangedSubview(testButton)
        stack.addArrangedSubview(statusRow)
    }

    private func makeCaption(_ text: String) -> UILabel {
        let label = UILabel()
        label.text = text
        label.font = .systemFont(ofSize: 13, weight: .semibold)
        label.textColor = .secondaryLabel
        return label
    }

    private func makeField(_ title: String,
                           _ field: UITextField,
                           placeholder: String,
                           keyboard: UIKeyboardType) -> UIStackView {
        field.borderStyle = .roundedRect
        field.placeholder = placeholder
        field.keyboardType = keyboard
        field.autocapitalizationType = .none
        field.autocorrectionType = .no
        field.clearButtonMode = .whileEditing
        field.returnKeyType = .done
        field.delegate = self
        field.heightAnchor.constraint(equalToConstant: 40).isActive = true

        let stack = UIStackView(arrangedSubviews: [makeCaption(title), field])
        stack.axis = .vertical
        stack.spacing = 6
        return stack
    }

    private func fillValues() {
        hostField.text = AppConfig.host
        portField.text = String(AppConfig.port)
        tokenField.text = AppConfig.token
        tokenSwitch.isOn = AppConfig.tokenEnabled
        tokenField.isEnabled = tokenSwitch.isOn
        statusLabel.text = "尚未测试"
    }

    // MARK: - 动作

    @objc private func tokenSwitchChanged() {
        tokenField.isEnabled = tokenSwitch.isOn
    }

    @objc private func cancelTapped() {
        onFinish?(false)
        dismiss(animated: true)
    }

    @objc private func saveTapped() {
        let host = (hostField.text ?? "").trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty else {
            showAlert("请先填写电脑 IP 地址")
            return
        }
        guard let port = parsedPort() else {
            showAlert("端口必须是 1-65535 的数字")
            return
        }
        AppConfig.host = host
        AppConfig.port = port
        AppConfig.token = tokenField.text ?? ""
        AppConfig.tokenEnabled = tokenSwitch.isOn

        onFinish?(true)
        dismiss(animated: true)
    }

    @objc private func testTapped() {
        let host = (hostField.text ?? "").trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty else {
            showAlert("请先填写电脑 IP 地址")
            return
        }
        guard let port = parsedPort() else {
            showAlert("端口必须是 1-65535 的数字")
            return
        }
        guard let url = AppConfig.statusURL(host: host, port: port) else {
            showAlert("地址格式不正确")
            return
        }

        setTesting()
        testButton.isEnabled = false

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 6
        if tokenSwitch.isOn {
            request.setValue(tokenField.text ?? "", forHTTPHeaderField: "X-Mobile-Token")
        }

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.testButton.isEnabled = true

                if let error = error {
                    self.setStatus(ok: false,
                                   text: "无法连接：\(error.localizedDescription)\n请检查 IP / 端口，并确认电脑服务已启动。")
                    return
                }
                let code = (response as? HTTPURLResponse)?.statusCode ?? -1
                let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                let ok = (200...299).contains(code) && body.contains("\"ok\"")
                let prefix = ok ? "连接成功（HTTP \(code)）" : "连接失败（HTTP \(code)）"
                self.setStatus(ok: ok, text: "\(prefix)\n\(body.prefix(200))")
            }
        }.resume()
    }

    // MARK: - 小工具

    private func parsedPort() -> Int? {
        let raw = (portField.text ?? "").trimmingCharacters(in: .whitespaces)
        if raw.isEmpty { return AppConfig.defaultPort }
        guard let port = Int(raw), (1...65535).contains(port) else { return nil }
        return port
    }

    private func setTesting() {
        statusDot.backgroundColor = .systemGray
        statusLabel.textColor = .secondaryLabel
        statusLabel.text = "正在测试连接…"
    }

    private func setStatus(ok: Bool, text: String) {
        statusDot.backgroundColor = ok ? .systemGreen : .systemRed
        statusLabel.textColor = .label
        statusLabel.text = text
    }

    private func showAlert(_ message: String) {
        let alert = UIAlertController(title: "提示", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "好", style: .default))
        present(alert, animated: true)
    }
}

// MARK: - UITextFieldDelegate

extension ConfigViewController: UITextFieldDelegate {
    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        textField.resignFirstResponder()
        return true
    }
}
