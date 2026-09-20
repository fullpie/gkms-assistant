#pragma once
#include <string_view>
namespace gkms::bridge {
// Current-PC CreateCardSelectorImplAsync branches, selected by the actual UI
// type. Paid cultivation customization is a different presenter/model.
constexpr int exam_card_selector_create_phase(std::string_view name){
    return name=="ProduceCardSelectorOverlayPresenter"?2:
        name=="ProduceUpgradeCardSelectorOverlayPresenter"?0:
        name=="ProduceChangeCardSelectorOverlayPresenter"?1:-1;
}
constexpr const char* exam_card_selector_create_awaiter(int phase){
    return phase==0?"<>u__1":phase==1?"<>u__2":phase==2?"<>u__3":nullptr;
}
}
