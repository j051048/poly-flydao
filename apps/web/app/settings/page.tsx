"use client";

import { useEffect, useState } from "react";

interface Settings {
  evmKey: string;
  baseUrl: string;
  apiKey: string;
  selectedModel: string;
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings>({
    evmKey: "",
    baseUrl: "https://api.openai.com/v1",
    apiKey: "",
    selectedModel: "",
  });
  
  const [models, setModels] = useState<string[]>([]);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  useEffect(() => {
    // Load saved settings
    try {
      const saved = localStorage.getItem("polybot_settings");
      if (saved) {
        setSettings(JSON.parse(saved));
      }
    } catch (e) {
      console.error("Failed to parse settings", e);
    }
  }, []);

  const handleChange = (field: keyof Settings, value: string) => {
    setSettings((prev) => ({ ...prev, [field]: value }));
  };

  const handleSave = () => {
    localStorage.setItem("polybot_settings", JSON.stringify(settings));
    alert("配置已保存到本地。");
  };

  const testConnectivity = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const url = settings.baseUrl.replace(/\/$/, "") + "/models";
      const res = await fetch(url, {
        headers: {
          Authorization: `Bearer ${settings.apiKey}`,
        },
      });

      if (!res.ok) {
        throw new Error(`HTTP Error ${res.status}: ${res.statusText}`);
      }

      const data = await res.json();
      if (data && data.data && Array.isArray(data.data)) {
        const modelNames = data.data.map((m: any) => m.id);
        setModels(modelNames);
        setTestResult({ success: true, message: `连通性测试成功，发现 ${modelNames.length} 个模型。` });
        
        // Auto select first if none selected
        if (!settings.selectedModel && modelNames.length > 0) {
          handleChange("selectedModel", modelNames[0]);
        }
      } else {
        throw new Error("响应格式不符合预期（缺失 data 数组）。");
      }
    } catch (error: any) {
      setTestResult({ success: false, message: `测试失败：${error.message}` });
    } finally {
      setTesting(false);
    }
  };

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <h1 style={{ fontSize: "24px", margin: 0 }}>系统配置</h1>
        </div>
      </header>

      <section className="panel controls-panel" style={{ maxWidth: "800px" }}>
        <div className="section-heading">
          <div>
            <p className="eyebrow">CONFIGURATION</p>
            <h2>个人参数设置</h2>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
          {/* EVM Key */}
          <div>
            <label className="field-label" htmlFor="evm-key">EVM 钱包私钥 (本地保存)</label>
            <div className="token-row">
              <input
                id="evm-key"
                type="password"
                value={settings.evmKey}
                onChange={(e) => handleChange("evmKey", e.target.value)}
                placeholder="0x..."
                autoComplete="off"
              />
            </div>
            <p className="field-help">用于在本地签署部分交易，不会上传至控制台服务器。</p>
          </div>

          {/* Base URL */}
          <div>
            <label className="field-label" htmlFor="base-url">第三方模型中转站 Base URL</label>
            <div className="token-row">
              <input
                id="base-url"
                type="text"
                value={settings.baseUrl}
                onChange={(e) => handleChange("baseUrl", e.target.value)}
                placeholder="https://api.openai.com/v1"
              />
            </div>
            <p className="field-help">提供 OpenAI 兼容的 /models 接口的中转站地址。</p>
          </div>

          {/* API Key */}
          <div>
            <label className="field-label" htmlFor="api-key">API Key</label>
            <div className="token-row">
              <input
                id="api-key"
                type="password"
                value={settings.apiKey}
                onChange={(e) => handleChange("apiKey", e.target.value)}
                placeholder="sk-..."
                autoComplete="off"
              />
              <button
                className="secondary-button"
                type="button"
                onClick={testConnectivity}
                disabled={!settings.baseUrl || !settings.apiKey || testing}
              >
                {testing ? "测试中..." : "测试连通性"}
              </button>
            </div>
            {testResult && (
              <div style={{ marginTop: "12px" }}>
                <div className={`notice ${testResult.success ? "success" : "error"}`}>
                  {testResult.message}
                </div>
              </div>
            )}
          </div>

          {/* Model Selection */}
          {models.length > 0 && (
            <div>
              <label className="field-label" htmlFor="model-select">默认模型选择</label>
              <div className="token-row">
                <select
                  id="model-select"
                  value={settings.selectedModel}
                  onChange={(e) => handleChange("selectedModel", e.target.value)}
                  style={{ width: "100%", padding: "12px 16px", borderRadius: "12px", background: "var(--surface)", border: "1px solid var(--line)", color: "var(--text)" }}
                >
                  <option value="" disabled>请选择模型</option>
                  {models.map((model) => (
                    <option key={model} value={model}>{model}</option>
                  ))}
                </select>
              </div>
            </div>
          )}

          <div style={{ marginTop: "16px", display: "flex", justifyContent: "flex-end" }}>
            <button
              className="primary-button"
              type="button"
              onClick={handleSave}
            >
              保存配置
            </button>
          </div>
        </div>
      </section>
    </main>
  );
}
