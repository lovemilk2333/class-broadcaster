// client-launcher 启动随包携带的 Windows Python runtime，不依赖系统 Python。
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
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

	plugins := filepath.Join(root, "Lib", "site-packages", "PySide6", "plugins")
	platforms := filepath.Join(plugins, "platforms")
	pyside := filepath.Join(root, "Lib", "site-packages", "PySide6")

	cmd := exec.Command(python, "-O", "-m", "client")
	cmd.Dir = root
	env := append(os.Environ(),
		"MKCB_INSTALL_DIR="+root,
		"PYTHONNOUSERSITE=1",
		"PYTHONDONTWRITEBYTECODE=1",
	)
	// Bundled PySide6 keeps qwindows.dll under plugins/platforms. Without these
	// paths, pythonw.exe often starts with no GUI on clean Windows machines.
	if _, err := os.Stat(platforms); err == nil {
		env = append(env,
			"QT_PLUGIN_PATH="+plugins,
			"QT_QPA_PLATFORM_PLUGIN_PATH="+platforms,
		)
	}
	if _, err := os.Stat(pyside); err == nil {
		env = append(env, "PATH="+pyside+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	cmd.Env = env
	configureProcess(cmd)
	if err := cmd.Run(); err != nil {
		showError(fmt.Sprintf("client runtime exited: %v", err))
	}
}
