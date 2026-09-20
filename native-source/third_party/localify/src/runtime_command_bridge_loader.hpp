#pragma once
namespace gkms::runtime_command_bridge_loader {
void pump() noexcept;
void* unique_player_loop_run() noexcept;
void pump_from_player_loop(void* instance,void* method) noexcept;
void record_player_loop_registration(int create_status,int enable_status) noexcept;
}
