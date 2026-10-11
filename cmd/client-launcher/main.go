// client-launcher 启动随包携带的 Windows Python runtime，不依赖系统 Python。
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func main() {
	executable, err := os.Executable()
	if err != nil {
		showError(err.Error())
		return
	}
	root := filepath.Dir(executable)
	python := filepath.Join(root, "runtime", "pythonw.exe")
	if _, err := os.Stat(python); err != nil {
		showError(fmt.Sprintf("embedded Python runtime is missing: %s", python))
		return
	}

	site := filepath.Join(root, "Lib", "site-packages")
	plugins := filepath.Join(site, "PySide6", "plugins")
	platforms := filepath.Join(plugins, "platforms")

	cmd := exec.Command(python, "-O", "-m", "client")
	cmd.Dir = root
	env := append(os.Environ(),
		"MKCB_INSTALL_DIR="+root,
		"PYTHONNOUSERSITE=1",
		"PYTHONDONTWRITEBYTECODE=1",
	)
	// Optional legacy NLTK_DATA (piper-plus/g2p_en removed; harmless if present).
	nltkData := filepath.Join(root, "Lib", "nltk_data")
	if info, err := os.Stat(nltkData); err == nil && info.IsDir() {
		env = append(env, "NLTK_DATA="+nltkData)
	}
	// Bundled PySide6 keeps qwindows.dll under plugins/platforms. Without these
	// paths, pythonw.exe often starts with no GUI on clean Windows machines.
	if _, err := os.Stat(platforms); err == nil {
		env = append(env,
			"QT_PLUGIN_PATH="+plugins,
			"QT_QPA_PLATFORM_PLUGIN_PATH="+platforms,
		)
	}
	// Native DLL search path for Qt + onnxruntime (classic piper-tts).
	// Missing these on a clean Windows box yields ACCESS_VIOLATION (0xC0000005)
	// when SpeechWorker imports onnxruntime — no Python traceback, process dies.
	pathDirs := collectNativeDLLDirs(root, site)
	if len(pathDirs) > 0 {
		env = append(env, "PATH="+strings.Join(pathDirs, string(os.PathListSeparator))+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	cmd.Env = env
	configureProcess(cmd)
	if err := cmd.Run(); err != nil {
		showError(fmt.Sprintf("client runtime exited: %v", err))
	}
}

// collectNativeDLLDirs returns existing directories that must precede PATH so
// LoadLibrary finds bundled Qt / onnxruntime DLLs.
func collectNativeDLLDirs(root, site string) []string {
	// onnxruntime/capi before PySide6: Qt ships msvcp140 14.29; onnxruntime 1.3x
	// needs ≥14.40. TTS child processes strip PySide6 + runtime/ from PATH and
	// preload System32 CRT — do not put runtime/ ahead of System32 here either
	// (embeddable vcruntime is often 14.38). Keep PySide6 for Qt plugins only.
	candidates := []string{
		filepath.Join(site, "onnxruntime", "capi"),
		filepath.Join(site, "numpy.libs"),
		filepath.Join(site, "PySide6"),
		filepath.Join(site, "shiboken6"),
		// runtime/ last: embeddable CPython needs its own vcruntime for python.exe,
		// but it must not beat System32 for onnxruntime (child PATH strips it).
		filepath.Join(root, "runtime"),
	}
	out := make([]string, 0, len(candidates))
	for _, dir := range candidates {
		if info, err := os.Stat(dir); err == nil && info.IsDir() {
			out = append(out, dir)
		}
	}
	return out
}
