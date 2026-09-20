#pragma once

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <errno.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <direct.h>
#endif

#include "minizip/unzip.h"
#include "GakumasLocalify/Log.h"

// 辅助函数：递归创建目录
static bool CreateDirectoryRecursively(const std::string& dir) {
    if (dir.empty())
        return false;

    // 尝试创建目录
#ifdef _WIN32
    int ret = _mkdir(dir.c_str());
#else
    int ret = mkdir(dir.c_str(), 0755);
#endif
    if (ret == 0 || errno == EEXIST)
        return true;

    // 如果创建失败，尝试先创建父目录
    size_t pos = dir.find_last_of("/\\");
    if (pos != std::string::npos) {
        std::string parentDir = dir.substr(0, pos);
        if (!CreateDirectoryRecursively(parentDir))
            return false;
#ifdef _WIN32
        ret = _mkdir(dir.c_str());
#else
        ret = mkdir(dir.c_str(), 0755);
#endif
        return (ret == 0 || errno == EEXIST);
    }
    return false;
}

// 解压缩函数
static bool UnzipFile(const std::string& zipPath, const std::string& destinationFolder) {
    // 打开zip文件
    unzFile zipfile = unzOpen(zipPath.c_str());
    if (zipfile == nullptr) {
        GakumasLocal::Log::ErrorFmt( "Can't open zip file: %s", zipPath.c_str());
        return false;
    }

    int ret = unzGoToFirstFile(zipfile);
    if (ret != UNZ_OK) {
        GakumasLocal::Log::ErrorFmt("Can't read first file %s", zipPath.c_str());
        unzClose(zipfile);
        return false;
    }

    // 遍历zip内的每个文件
    do {
        char filename[512] = { 0 };
        unz_file_info fileInfo;
        ret = unzGetCurrentFileInfo(zipfile, &fileInfo,
            filename, sizeof(filename),
            nullptr, 0, nullptr, 0);
        if (ret != UNZ_OK) {
            GakumasLocal::Log::ErrorFmt("Read ZIP File Info Error");
            unzClose(zipfile);
            return false;
        }

        std::string filePath = filename;
        std::string fullPath = destinationFolder;
        // 保证目标目录以路径分隔符结尾
        if (fullPath.back() != '/' && fullPath.back() != '\\') {
            fullPath += "/";
        }
        fullPath += filePath;

        // 判断是否为目录（目录条目通常以'/'结尾）
        if (!filePath.empty() && (filePath.back() == '/' || filePath.back() == '\\')) {
            // 创建目录
            if (!CreateDirectoryRecursively(fullPath)) {
                GakumasLocal::Log::ErrorFmt("Create Dir Failed: %s", fullPath.c_str());
                unzClose(zipfile);
                return false;
            }
        }
        else {
            // 对于文件，先确保其上级目录存在
            size_t pos = fullPath.find_last_of("/\\");
            if (pos != std::string::npos) {
                std::string directory = fullPath.substr(0, pos);
                if (!CreateDirectoryRecursively(directory)) {
                    GakumasLocal::Log::ErrorFmt("Create Dir Failed: %s", directory.c_str());
                    unzClose(zipfile);
                    return false;
                }
            }

            // 打开zip中文件
            ret = unzOpenCurrentFile(zipfile);
            if (ret != UNZ_OK) {
                GakumasLocal::Log::ErrorFmt("Open file in zip failed: %s", filePath.c_str());
                unzClose(zipfile);
                return false;
            }

            // 在目标路径上创建新文件
            FILE* outFile = fopen(fullPath.c_str(), "wb");
            if (outFile == nullptr) {
                GakumasLocal::Log::ErrorFmt("Can't create output file: %s", fullPath.c_str());
                unzCloseCurrentFile(zipfile);
                unzClose(zipfile);
                return false;
            }

            // 读取数据并写入文件
            const int bufferSize = 8192;
            std::vector<char> buffer(bufferSize);
            int bytesRead = 0;
            do {
                bytesRead = unzReadCurrentFile(zipfile, buffer.data(), bufferSize);
                if (bytesRead < 0) {
                    GakumasLocal::Log::ErrorFmt("Read File Error: %s", filePath.c_str());
                    fclose(outFile);
                    unzCloseCurrentFile(zipfile);
                    unzClose(zipfile);
                    return false;
                }
                if (bytesRead > 0) {
                    fwrite(buffer.data(), 1, bytesRead, outFile);
                }
            } while (bytesRead > 0);

            fclose(outFile);
            unzCloseCurrentFile(zipfile);
        }

        ret = unzGoToNextFile(zipfile);
    } while (ret == UNZ_OK);

    unzClose(zipfile);
    return true;
}


static bool UnzipFile(const std::string& zipPath, const std::string& destinationFolder, std::string_view targetDir) {
	if (targetDir.empty()) {
		return UnzipFile(zipPath, destinationFolder);
	}

    // 打开 zip 文件
    unzFile zipfile = unzOpen(zipPath.c_str());
    if (zipfile == nullptr) {
        GakumasLocal::Log::ErrorFmt("Can't open zip file: %s", zipPath.c_str());
        return false;
    }

    // 用于标识：ZIP 根目录是否直接包含 targetDir 文件夹
    bool targetAtRoot = false;
    // 如果在根目录下没找到，再尝试记录哪个根目录下包含 targetDir（只检查一层）
    std::string candidateRoot;

    // 第一遍扫描：遍历所有条目，判断目标文件夹所在位置
    int ret = unzGoToFirstFile(zipfile);
    if (ret != UNZ_OK) {
        GakumasLocal::Log::ErrorFmt("Can't read first file: %s", zipPath.c_str());
        unzClose(zipfile);
        return false;
    }
    do {
        char filename[512] = { 0 };
        unz_file_info fileInfo{};
        ret = unzGetCurrentFileInfo(zipfile, &fileInfo,
            filename, sizeof(filename),
            nullptr, 0, nullptr, 0);
        if (ret != UNZ_OK) {
            GakumasLocal::Log::ErrorFmt("Read ZIP File Info Error");
            unzClose(zipfile);
            return false;
        }
        std::string entryName = filename;
        // 将 '\' 统一替换为 '/'
        std::replace(entryName.begin(), entryName.end(), '\\', '/');

        // 若条目全名等于 targetDir（可以带或不带结尾 '/'），且没有前缀，则认为在根目录
        if ((entryName == targetDir || entryName == std::string(targetDir) + "/") &&
            entryName.find('/') == entryName.size() - 1) {
            targetAtRoot = true;
        }
        else {
            // 对于含有路径分隔符的条目，提取第一级目录
            if (auto pos = entryName.find('/'); pos != std::string::npos) {
                std::string root = entryName.substr(0, pos);
                // remainder 为 root 后面的路径
                std::string remainder = entryName.substr(pos + 1);
                // 仅检查第一层目录
                auto pos2 = remainder.find('/');
                std::string firstSubDir = (pos2 != std::string::npos) ? remainder.substr(0, pos2) : remainder;
                if (firstSubDir == targetDir) {
                    candidateRoot = root;
                }
            }
        }
        ret = unzGoToNextFile(zipfile);
    } while (ret == UNZ_OK);

    // 根据扫描结果确定 extractionPrefix
    // 若目标文件夹在根目录，则仅解压出 targetDir 内的条目（写入时去掉 targetDir 前缀）
    // 否则，若 candidateRoot 不为空，则解压 candidateRoot 内的所有条目（写入时去除 candidateRoot 前缀）
    std::string extractionPrefix;
    if (targetAtRoot) {
        extractionPrefix = std::string(targetDir) + "/";
    }
    else if (!candidateRoot.empty()) {
        extractionPrefix = candidateRoot + "/";
    }
    else {
        GakumasLocal::Log::ErrorFmt("Target directory '%.*s' not found in zip", (int)targetDir.size(), targetDir.data());
        unzClose(zipfile);
        return false;
    }

    // 重新打开 zip 文件进行解压（或重置扫描位置）
    unzClose(zipfile);
    zipfile = unzOpen(zipPath.c_str());
    if (zipfile == nullptr) {
        GakumasLocal::Log::ErrorFmt("Can't reopen zip file: %s", zipPath.c_str());
        return false;
    }
    ret = unzGoToFirstFile(zipfile);
    if (ret != UNZ_OK) {
        GakumasLocal::Log::ErrorFmt("Can't read first file: %s", zipPath.c_str());
        unzClose(zipfile);
        return false;
    }

    // 第二遍扫描：仅解压属于指定根目录内的条目
    do {
        char filename[512] = { 0 };
        unz_file_info fileInfo{};
        ret = unzGetCurrentFileInfo(zipfile, &fileInfo,
            filename, sizeof(filename),
            nullptr, 0, nullptr, 0);
        if (ret != UNZ_OK) {
            GakumasLocal::Log::ErrorFmt("Read ZIP File Info Error");
            unzClose(zipfile);
            return false;
        }
        std::string entryName = filename;
        std::replace(entryName.begin(), entryName.end(), '\\', '/');

        // 仅处理以 extractionPrefix 开头的条目
        if (entryName.rfind(extractionPrefix, 0) == 0) {
            // 去除 extractionPrefix 得到相对路径
            std::string relativePath = entryName.substr(extractionPrefix.size());

            // 构造输出全路径
            std::string fullPath = destinationFolder;
            if (!fullPath.empty() && fullPath.back() != '/' && fullPath.back() != '\\')
                fullPath += "/";
            fullPath += relativePath;

            // 如果 relativePath 为空或以 '/' 结尾，认为是目录
            if (relativePath.empty() || relativePath.back() == '/') {
                if (!CreateDirectoryRecursively(fullPath)) {
                    GakumasLocal::Log::ErrorFmt("Create Dir Failed: %s", fullPath.c_str());
                    unzClose(zipfile);
                    return false;
                }
            }
            else {
                // 确保上级目录存在
                if (auto pos = fullPath.find_last_of("/\\"); pos != std::string::npos) {
                    std::string directory = fullPath.substr(0, pos);
                    if (!CreateDirectoryRecursively(directory)) {
                        GakumasLocal::Log::ErrorFmt("Create Dir Failed: %s", directory.c_str());
                        unzClose(zipfile);
                        return false;
                    }
                }
                ret = unzOpenCurrentFile(zipfile);
                if (ret != UNZ_OK) {
                    GakumasLocal::Log::ErrorFmt("Open file in zip failed: %s", entryName.c_str());
                    unzClose(zipfile);
                    return false;
                }
                FILE* outFile = fopen(fullPath.c_str(), "wb");
                if (outFile == nullptr) {
                    GakumasLocal::Log::ErrorFmt("Can't create output file: %s", fullPath.c_str());
                    unzCloseCurrentFile(zipfile);
                    unzClose(zipfile);
                    return false;
                }
                constexpr int bufferSize = 8192;
                std::vector<char> buffer(bufferSize);
                int bytesRead = 0;
                while ((bytesRead = unzReadCurrentFile(zipfile, buffer.data(), bufferSize)) > 0) {
                    if (fwrite(buffer.data(), 1, bytesRead, outFile) != static_cast<size_t>(bytesRead)) {
                        GakumasLocal::Log::ErrorFmt("Write File Error: %s", fullPath.c_str());
                        fclose(outFile);
                        unzCloseCurrentFile(zipfile);
                        unzClose(zipfile);
                        return false;
                    }
                }
                if (bytesRead < 0) {
                    GakumasLocal::Log::ErrorFmt("Read File Error: %s", entryName.c_str());
                    fclose(outFile);
                    unzCloseCurrentFile(zipfile);
                    unzClose(zipfile);
                    return false;
                }
                fclose(outFile);
                unzCloseCurrentFile(zipfile);
            }
        }
        ret = unzGoToNextFile(zipfile);
    } while (ret == UNZ_OK);
    unzClose(zipfile);
    return true;
}
