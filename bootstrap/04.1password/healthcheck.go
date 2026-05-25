package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

type dependencyList []string

func (d *dependencyList) String() string {
	return strings.Join(*d, ",")
}

func (d *dependencyList) Set(value string) error {
	if !strings.Contains(value, ":") {
		return fmt.Errorf("dependency must use service:status format")
	}
	*d = append(*d, value)
	return nil
}

type healthResponse struct {
	Name         string       `json:"name"`
	Version      string       `json:"version"`
	Dependencies []dependency `json:"dependencies"`
}

type dependency struct {
	Service string `json:"service"`
	Status  string `json:"status"`
	Message string `json:"message"`
}

func main() {
	var required dependencyList
	url := flag.String("url", "http://127.0.0.1:8080/health", "1Password Connect health URL")
	timeout := flag.Duration("timeout", 5*time.Second, "HTTP timeout")
	flag.Var(&required, "dependency", "required dependency in service:status format; may be repeated")
	flag.Parse()

	client := http.Client{Timeout: *timeout}
	resp, err := client.Get(*url)
	if err != nil {
		fmt.Fprintf(os.Stderr, "health request failed: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "unexpected health status: %d\n", resp.StatusCode)
		os.Exit(1)
	}

	var body healthResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		fmt.Fprintf(os.Stderr, "invalid health response: %v\n", err)
		os.Exit(1)
	}

	statusByService := map[string]string{}
	for _, dep := range body.Dependencies {
		statusByService[dep.Service] = dep.Status
	}

	for _, item := range required {
		service, want, _ := strings.Cut(item, ":")
		got, ok := statusByService[service]
		if !ok {
			fmt.Fprintf(os.Stderr, "missing dependency %q in health response\n", service)
			os.Exit(1)
		}
		if got != want {
			fmt.Fprintf(os.Stderr, "dependency %q status = %q, want %q\n", service, got, want)
			os.Exit(1)
		}
	}

	fmt.Printf("%s %s healthy\n", body.Name, body.Version)
}
