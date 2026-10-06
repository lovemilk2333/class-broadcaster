//go:build !windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

// waitForProcess 在 Unix 使用 signal 0 检查旧进程，避免 Wait 只能等待子进程。
func waitForProcess(pid int) error {
	process, err := os.FindProcess(pid)
	if err != nil {
		return err
	}
	for attempt := 0; attempt < 120; attempt++ {
		err := process.Signal(syscall.Signal(0))
		if errors.Is(err, syscall.ESRCH) {
			return nil
		}
		if err != nil && !errors.Is(err, syscall.EPERM) {
			return err
		}
		time.Sleep(250 * time.Millisecond)
	}
	return errors.New("timed out waiting for old process to exit")
}

func startDetachedProcess(executable, workDir string) error {
	cmd := exec.Command(executable)
	cmd.Dir = workDir
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.Stdin = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	if cmd.Process != nil {
		_ = cmd.Process.Release()
	}
	return nil
}

func replaceFileWithRetry(destination string, data []byte, mode os.FileMode) error {
	temporary := destination + ".mkcb-new-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	if err := os.WriteFile(temporary, data, mode|0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, destination); err != nil {
		_ = os.Remove(destination)
		if err2 := os.Rename(temporary, destination); err2 != nil {
			_ = os.Remove(temporary)
			return fmt.Errorf("replace %s: %w", destination, err2)
		}
	}
	return nil
}
