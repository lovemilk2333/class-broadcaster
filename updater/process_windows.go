//go:build windows

package main

import (
	"errors"
	"os/exec"
	"syscall"
)

const windowsErrorInvalidParameter syscall.Errno = 87

// CREATE_* flags so the restarted client survives updater exit without a console flash.
const (
	createNewProcessGroup = 0x00000200
	detachedProcess       = 0x00000008
	createNoWindow        = 0x08000000
)

// waitForProcess 在 Windows 直接等待系统进程句柄，避免 Signal(nil) 不可用。
func waitForProcess(pid int) error {
	handle, err := syscall.OpenProcess(syscall.SYNCHRONIZE|syscall.PROCESS_QUERY_INFORMATION, false, uint32(pid))
	if err != nil {
		var errno syscall.Errno
		if errors.As(err, &errno) && errno == windowsErrorInvalidParameter {
			return nil
		}
		return err
	}
	defer syscall.CloseHandle(handle)
	// Client exits shortly after launching the updater; allow enough time for
	// graceful Qt shutdown and worker teardown (up to ~2 minutes).
	const timeoutMs = 120_000
	result, err := syscall.WaitForSingleObject(handle, timeoutMs)
	if err != nil {
		return err
	}
	if result == syscall.WAIT_TIMEOUT {
		return errors.New("timed out waiting for old process to exit")
	}
	return nil
}

func startDetachedProcess(executable, workDir string) error {
	cmd := exec.Command(executable)
	cmd.Dir = workDir
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.Stdin = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: createNewProcessGroup | detachedProcess | createNoWindow,
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	// Detach from the child so the updater can exit immediately.
	if cmd.Process != nil {
		_ = cmd.Process.Release()
	}
	return nil
}
