//go:build !windows

package main

import (
	"fmt"
	"os/exec"
)

func configureProcess(_ *exec.Cmd) {}

func showError(message string) {
	fmt.Println(message)
}
