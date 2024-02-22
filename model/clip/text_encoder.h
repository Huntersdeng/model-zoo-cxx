#pragma once
#include "model/base/model.h"
#include "model/clip/text_tokenizer.h"

namespace clip {
class TextEncoder : public Model {
   public:
    TextEncoder() = delete;
    TextEncoder(const std::string &model_path, const std::string framework_type, const std::string &bpe_path);
    TextEncoder(const std::string &yaml_file);
    virtual ~TextEncoder();
    void setPrompt(const std::vector<std::string> &texts);
    void forward(const std::vector<std::string> &texts, IOTensor &features);

    size_t input_size() const { return m_input_size_; }
    size_t output_size() const { return m_output_size_; }

   protected:
    void preprocess(const std::vector<std::string> &texts, IOTensor &text_embeddings);

   private:
    bool online = true;
    std::shared_ptr<TextTokenizer> m_tokenizer_;
    size_t m_input_size_;
    size_t m_output_size_;
    std::map<std::string, std::vector<float>> m_encoder_;
};
}