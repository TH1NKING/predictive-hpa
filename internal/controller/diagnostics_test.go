package controller

import (
	"bytes"
	"encoding/json"
	"sync"
	"time"
)

// Capture the public JSON log stream emitted by the running test manager.
// Writes and snapshots can occur concurrently with a reconciliation.
var controllerDiagnosticLog diagnosticLogBuffer

type diagnosticLogBuffer struct {
	sync.Mutex
	buffer bytes.Buffer
}

func (b *diagnosticLogBuffer) Write(data []byte) (int, error) {
	b.Lock()
	defer b.Unlock()
	return b.buffer.Write(data)
}

func (b *diagnosticLogBuffer) records(namespace string) []map[string]any {
	b.Lock()
	data := bytes.Clone(b.buffer.Bytes())
	b.Unlock()
	var records []map[string]any
	for line := range bytes.SplitSeq(data, []byte("\n")) {
		var record map[string]any
		if json.Unmarshal(line, &record) == nil && record["namespace"] == namespace {
			records = append(records, record)
		}
	}
	return records
}

func diagnosticTime(record map[string]any, field string) time.Time {
	value, _ := record[field].(string)
	stamp, _ := time.Parse(time.RFC3339Nano, value)
	return stamp
}
