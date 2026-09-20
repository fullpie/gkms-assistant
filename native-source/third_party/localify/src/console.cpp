#include "stdinclude.hpp"


namespace
{
	void console_thread()
	{
		std::string line;

		while (true)
		{
			std::cin >> line;

			std::cout << "\n] " << line << "\n";
		}
	}
}

void start_console()
{
#ifdef _DEBUG
	std::thread(console_thread).detach();
#endif
}