//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"
	"unsafe"
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

var (
	modKernel32     = syscall.NewLazyDLL("kernel32.dll")
	procMoveFileExW = modKernel32.NewProc("MoveFileExW")
)

const (
	moveFileReplaceExisting = 0x1
	moveFileWriteThrough    = 0x8
)

func moveFileEx(src, dst string) error {
	from, err := syscall.UTF16PtrFromString(src)
	if err != nil {
		return err
	}
	to, err := syscall.UTF16PtrFromString(dst)
	if err != nil {
		return err
	}
	r1, _, callErr := procMoveFileExW.Call(
		uintptr(unsafe.Pointer(from)),
		uintptr(unsafe.Pointer(to)),
		uintptr(moveFileReplaceExisting|moveFileWriteThrough),
	)
	if r1 == 0 {
		if callErr != nil {
			return callErr
		}
		return errors.New("MoveFileExW failed")
	}
	return nil
}

// replaceFileWithRetry writes via a sibling temp file, then renames with retries.
// Windows often keeps Qt/Python DLLs mapped briefly after process exit; identical
// content is skipped by the caller, and here we retry remove+MoveFileEx.
func replaceFileWithRetry(destination string, data []byte, mode os.FileMode) error {
	temporary := destination + ".mkcb-new-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	if err := os.WriteFile(temporary, data, mode|0o600); err != nil {
		return err
	}
	var lastErr error
	for attempt := 0; attempt < 40; attempt++ {
		if attempt > 0 {
			time.Sleep(250 * time.Millisecond)
		}
		// Prefer MoveFileEx (atomic replace when possible).
		if err := moveFileEx(temporary, destination); err == nil {
			return nil
		} else {
			lastErr = err
		}
		// Fall back: move locked destination aside, then rename temp into place.
		stale := destination + ".mkcb-old-" + strconv.FormatInt(time.Now().UnixNano(), 10)
		if renameErr := os.Rename(destination, stale); renameErr == nil {
			if err := os.Rename(temporary, destination); err == nil {
				_ = os.Remove(stale)
				return nil
			} else {
				lastErr = err
				_ = os.Rename(stale, destination) // best-effort restore
			}
		} else {
			_ = os.Remove(destination)
			if err := os.Rename(temporary, destination); err == nil {
				return nil
			} else {
				lastErr = err
			}
		}
	}
	_ = os.Remove(temporary)
	if lastErr == nil {
		lastErr = errors.New("replace failed after retries")
	}
	return fmt.Errorf("replace %s: %w", destination, lastErr)
}
