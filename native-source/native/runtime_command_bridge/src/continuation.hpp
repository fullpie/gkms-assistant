#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
// Input rows are native PlayLog/Command getter observations. Select logs and
// automatic commands do not replace the latest original manual command.
json latest_manual_main_command(const json& observed_logs);
// One child selector input remains part of its submitted Exam parent's
// transaction. This validates evidence; it creates no owner/state machine.
void validate_exam_continuation(const json& request,const json& snapshot,const json& parent,const std::string& generation);
}
