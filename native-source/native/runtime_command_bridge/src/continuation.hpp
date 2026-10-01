#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
// Input rows are native PlayLog/Command getter observations. Select logs and
// automatic commands do not replace the latest original manual command.
json latest_manual_main_command(const json& observed_logs);
// One child selector input remains part of its submitted Exam parent's
// transaction. This validates evidence; it creates no owner/state machine.
void validate_exam_continuation(const json& request,const json& snapshot,const json& parent,const std::string& generation);
// The existing foreground callback may interrupt an already dispatched parent.
// This proves callback admission only, never parent success or async settlement.
void validate_error_return_target(const json& request,const json& snapshot);
void validate_error_return_continuation(const json& request,const json& snapshot,const json& parent,const std::string& generation);
void reject_mutation_behind_error(const std::string& command,const json& error_snapshot);
}
