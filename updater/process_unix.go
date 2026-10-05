//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
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
